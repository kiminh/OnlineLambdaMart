import numpy as np
import lightgbm as gbm
import matplotlib.pyplot as plt
import os
import sys
import timeit
from collections import defaultdict

from oltr.utils.metric import ndcg_at_k
from oltr.utils.click_simulator import DependentClickModel
from oltr.utils.queries import Queries, find_constant_features
from oltr.rankers import LinRanker, LMARTRanker, ClickLMARTRanker

TRAIN_PATH = os.path.expanduser('~/data/web30k/Fold1/train.txt')
VALID_PATH = os.path.expanduser('~/data/web30k/Fold1/vali.txt')
TEST_PATH = os.path.expanduser('~/data/web30k/Fold1/test.txt')
NUM_QUERIES_FOR_CLICK_LMART = 10 ** 4

class OnlineLTR(object):

  def __init__(self, train_qset, valid_qset=None, test_qset=None, seed=42):

    self.seed = seed
    np.random.seed(seed)

    self.train_qset = train_qset
    cls = find_constant_features(self.train_qset)
    self.train_qset.adjust(remove_features=cls, purge=True, scale=True)

    self.valid_qset = valid_qset
    cls = find_constant_features(self.valid_qset)
    self.valid_qset.adjust(remove_features=cls, purge=True, scale=True)

    self.test_qset = test_qset
    cls = find_constant_features(self.test_qset)
    self.test_qset.adjust(remove_features=cls, purge=True, scale=True)

    # The previous collected training date.
    self.observed_training_data = []

  def sample_query_ids(self, num_queries, data='train'):
    qset = self.train_qset
    if data == 'valid':
      qset = self.valid_qset
    if data == 'test':
      qset = self.test_qset
    return np.random.choice(qset.n_queries, num_queries)

  def get_labels_and_rankings(self, ranker, num_queries):
    """Apply a ranker to a subsample of the data and get the labels and ranks.

    Args:
      ranker: A LightGBM model.
      num_queries: Number of queries to be sampled from self.train_qset

    Returns:
      A tuple of lists that assign labels and rankings to the documents of each
      query.
    """
    query_ids = self.sample_query_ids(num_queries)
    n_docs_per_query = [self.train_qset[qid].document_count() for qid in query_ids]
    indices = [0] + np.cumsum(n_docs_per_query).tolist()
    labels = [self.train_qset[qid].relevance_scores for qid in query_ids]

    # Get the rankings of document per query
    if ranker is None:
      rankings = [np.random.permutation(n_docs) for n_docs in n_docs_per_query]
    else:
      features = self.train_qset[query_ids].feature_vectors
      scores = ranker.predict(features)
      tie_breakers = np.random.rand(scores.shape[0])
      rankings = [np.lexsort((tie_breakers[indices[i]:indices[i+1]],
                              -scores[indices[i]:indices[i+1]]))
                  for i in range(num_queries)]

    return query_ids, labels, rankings

  def apply_click_model_to_labels_and_scores(self, click_model, labels,
                                             rankings):
    """This method samples some queries and generates clicks for them based on
    a click model.

    Args:
      click_model: a click model
      labels: true labels of documents
      rankings: ranking of documents of each query

    Returns:
      A list of clicks of documents of each query
    """
    clicks = [click_model.get_click(labels[i][rankings[i]])
              for i in range(len(rankings))]
    return clicks

  def generate_training_data_from_clicks(self, query_ids, clicks, rankings):
    """This method uses the clicks generated by
    apply_click_model_to_labels_and_scores to create a training dataset.

    Args:
      query_ids: the sampled query ids
      clicks: clicks from the click model

    Returns:
      A tuple of (train_features, train_labels):
        train_features: list of observed docs per query
        train_labels: list of click feedback per query
    """
    # last observed position of each ranking
    last_pos = []
    train_labels = []
    for click in clicks:
      if sum(click) == 0:
        last_pos.append(len(click))
      else:
        last_pos.append(np.where(click)[0][-1]+1)
      train_labels.append(click[:last_pos[-1]])

    train_indices = [self.train_qset.query_indptr[qid] + rankings[i][:last_pos[i]]
                     for i, qid in enumerate(query_ids)]
    train_features = [self.train_qset.feature_vectors[idx] for idx in train_indices]

    # Cf. the following for an example:
    # https://mlexplained.com/2019/05/27/learning-to-rank-explained-with-code/
    train_q_list_sizes = [feature.shape[0] for feature in train_features]

    train_features = np.concatenate(train_features)
    train_labels = np.concatenate(train_labels)

    self.observed_training_data.append((train_indices, train_labels,
                                        train_q_list_sizes))

  def update_ranker(self, ranker_params, fit_params):
    """"This method uses the training data from
    generate_training_data_from_clicks to improve the ranker."""
    if self.observed_training_data:
      train_indices = [inds for otd in self.observed_training_data
                       for inds in otd[0]]
      train_features = np.concatenate([self.train_qset.feature_vectors[inds]
                                       for inds in train_indices])
      train_labels = np.concatenate([otd[1]
                                     for otd in self.observed_training_data])
      train_q_list_sizes = np.concatenate([otd[2]
                                       for otd in self.observed_training_data])
    else:
      raise ValueError('OnlineLTR.generate_training_data_from_clicks()'
        'should be called before OnlineLTR.update_ranker().')

    ranker = gbm.LGBMRanker(**ranker_params)
    if 'early_stopping_rounds' in fit_params:
      num_queries = len(self.observed_training_data[-1][0])
      valid_query_ids = self.sample_query_ids(num_queries, data='valid')
      valid_labels = np.concatenate([self.valid_qset[qid].relevance_scores
                                     for qid in valid_query_ids])
      valid_features = self.valid_qset[valid_query_ids].feature_vectors
      valid_q_list_sizes = [self.valid_qset[qid].document_count() for qid in valid_query_ids]
      ranker.fit(X=train_features, y=train_labels, group=train_q_list_sizes,
                 eval_set=[(valid_features, valid_labels)], eval_group=[valid_q_list_sizes],
                 **fit_params)
    else:
      ranker.fit(X=train_features, y=train_labels, group=train_q_list_sizes,
                 **fit_params)
    return ranker

  def update_learner(self, ranker, num_train_queries, click_model,
                     ranker_params, fit_params):
    # Collect feedback
    train_query_ids, train_labels, train_rankings = \
      self.get_labels_and_rankings(ranker, num_train_queries)
    train_clicks = self.apply_click_model_to_labels_and_scores(
      click_model, train_labels, train_rankings)
    self.generate_training_data_from_clicks(
      train_query_ids, train_clicks, train_rankings)

    # Return retrained ranker
    return self.update_ranker(ranker_params, fit_params)

  def evaluate_ranker(self, ranker, eval_params, query_ids=None, data='test'):
    """ Evaluate the ranker based on the queries in self.train_qset
    :param ranker:
    :param eval_params:  ndcg, cutoff
    :return:
    """
    qset = self.test_qset
    if data == 'train':
      qset = self.train_qset
    if data == 'valid':
      qset = self.valid_qset
    if query_ids is None:
      eval_qset = qset
    else:
      eval_qset = qset[query_ids]
    scores = ranker.predict(eval_qset.feature_vectors)
    tie_breakers = np.random.rand(scores.shape[0])

    indices = eval_qset.query_indptr
    rankings = [np.lexsort((tie_breakers[indices[i]:indices[i + 1]],
                -scores[indices[i]:indices[i + 1]]))
                for i in range(eval_qset.n_queries)]
    # raise ValueError
    ndcgs = [eval_params['metric'](
                eval_qset[qid].relevance_scores[rankings[qid]],
                eval_params['cutoff'])
             for qid in range(eval_qset.n_queries)]
    return np.mean(ndcgs)

class ExploreThenExploitOLTR(OnlineLTR):

  def __init__(self, train_qset, num_explore_iterations, 
               valid_qset=None, test_qset=None, seed=42):
    super(ExploreThenExploitOLTR, 
          self).__init__(train_qset=train_qset, valid_qset=valid_qset,
                         test_qset=test_qset, seed=seed)
    self.num_explore_iterations = num_explore_iterations
    self.iteration = 0

  def get_labels_and_rankings(self, ranker, num_queries):
    if self.iteration < self.num_explore_iterations:
      ranker_ = None
    else:
      ranker_ = ranker
    return super(ExploreThenExploitOLTR,
                 self).get_labels_and_rankings(ranker_, num_queries)

  def update_ranker(self, ranker_params, fit_params):
    self.iteration += 1
    return super(ExploreThenExploitOLTR,
                 self).update_ranker(ranker_params, fit_params)

class Data:
  def __init__(self, train_path, valid_path, test_path):
    if train_path.endswith('.txt'):
      try:
        print('Data: Loading data from', train_path[:-4])
        self.train_qset = Queries.load(train_path[:-4])
      except FileNotFoundError:
        print('Data: Loading data from', train_path)
        self.train_qset = Queries.load_from_text(train_path)
        self.train_qset.save(train_path[:-4])
    if valid_path.endswith('.txt'):
      try:
        print('Data: Loading data from', valid_path[:-4])
        self.valid_qset = Queries.load(valid_path[:-4])
      except FileNotFoundError:
        print('Data: Loading data from', valid_path)
        self.valid_qset = Queries.load_from_text(valid_path)
        self.valid_qset.save(valid_path[:-4])
    if test_path.endswith('.txt'):
      try:
        print('Data: Loading data from', test_path[:-4])
        self.test_qset = Queries.load(test_path[:-4])
      except FileNotFoundError:
        print('Data: Loading data from', test_path)
        self.test_qset = Queries.load_from_text(test_path)
        self.test_qset.save(test_path[:-4])


def oltr_loop(data_path, num_iterations=20, num_train_queries=5, num_test_queries=100):
  oltr_ranker_params = {
    'min_child_samples': 50,
    'min_child_weight': 0,
    'n_estimators': 500,
    'learning_rate': 0.02,
    'num_leaves': 400,
    'boosting_type': 'gbdt',
    'objective': 'lambdarank',
  }
  oltr_fit_params = {
    'early_stopping_rounds': 50,
    'eval_metric': 'ndcg',
    'eval_at': 5,
    'verbose': 100,
  }
  eval_params = {
    'metric': ndcg_at_k,
    'cutoff': 10
  }
  data = Data(TRAIN_PATH, VALID_PATH, TEST_PATH)
  lmart_ranker_params = {
    'min_child_samples': 50,
    'min_child_weight': 0,
    'n_estimators': 500,
    'learning_rate': 0.02,
    'num_leaves': 400,
    'boosting_type': 'gbdt',
    'objective': 'lambdarank',
  }
  lmart_fit_params = {
    'early_stopping_rounds': 50,
    'eval_metric': 'ndcg',
    'eval_at': 5,
    'verbose': 50,
  }
  click_model = DependentClickModel(user_type='pure_cascade')

  ##########################################
  # This is for debugging in Chang's laptop
  ##########################################
  # train_path = data_path
  # valid_path = data_path
  # test_path = data_path
  ##########################################

  # Online learners
  online_learners = {
    # Follow the Leader
    'FTL': OnlineLTR(data.train_qset, data.valid_qset, data.test_qset),
  }
  for num_explore in range(num_iterations):
    online_learners['EtE %d' % num_explore] = ExploreThenExploitOLTR(
      data.train_qset, num_explore, data.valid_qset, data.test_qset)
  # online_learners['FTL'] = OnlineLTR(data.train_qset, data.valid_qset, data.test_qset)
  online_rankers = {lname:None for lname in online_rankers}
  # online_rankers['FTL'] = None
  offline_rankers = {
    'Linear': LinRanker(num_features=136),
    'Offline LambdaMART': LMARTRanker(
      data.train_qset, data.valid_qset, data.test_qset,
      lmart_ranker_params, lmart_fit_params),
    'Click LambdaMART': LMARTRanker(
      data.train_qset, data.valid_qset, data.test_qset,
      lmart_ranker_params, lmart_fit_params,
      total_number_of_clicked_queries=num_iterations * num_train_queries),
  }
  eval_results = defaultdict(list)

  for ind in range(num_iterations):
    # Train OLTR
    for lname in online_learners:
      online_rankers[lname] = online_learners[lname].update_learner(
        online_rankers[lname], num_train_queries, click_model,
        oltr_ranker_params, oltr_fit_params)

    # Evaluation
    test_query_ids = online_learners['FTL'].sample_query_ids(num_test_queries,
                                                             data='test')
    # Online
    for lname in online_learners:
      oltr_eval_value = online_learners[lname].evaluate_ranker(
        online_rankers[lname], eval_params, query_ids=test_query_ids)
      eval_results[lname].append(oltr_eval_value)
    # Offline (baselines)
    for offline_model_name, ranker in offline_rankers.items():
      eval_result = online_learners['FTL'].evaluate_ranker(ranker, eval_params,
                                                           query_ids=test_query_ids)
      eval_results[offline_model_name].append(eval_result)

    print('>>>>>>>>>>iteration: ', ind)
    print('Offline LambdaMART (headroom) performance : ',
          eval_results['Offline LambdaMART'][-1])
    print('Online LTR performance: ', eval_results['OLTR'][-1])
    print('Linear ranker (baseline) performance: ', eval_results['Linear'][-1])
  return eval_results


def plot_eval_results(eval_results, out_path='/tmp/plot.png'):
  fig, ax = plt.subplots()
  for ranker, metrics in eval_results.items():
    ax.plot(metrics, label=ranker)
  print('Saving a plot of the results to', out_path)
  plt.legend(loc='upper left')
  fig.savefig(out_path)


if __name__ == '__main__':
  num_iterations = 10
  num_train_queries = 5
  num_test_queries = 100
  oltr_data_path = 'data/mslr_fold1_train_sample.txt'
  if len(sys.argv) > 1:
    oltr_data_path = TRAIN_PATH
    num_iterations = int(sys.argv[1])
  if len(sys.argv) > 2:
    num_train_queries = int(sys.argv[2])
  if len(sys.argv) > 3:
    num_test_queries = int(sys.argv[3])
  start = timeit.default_timer()
  eval_results = oltr_loop(oltr_data_path, num_iterations, num_train_queries, num_test_queries)
  plot_eval_results(eval_results,
    out_path='/tmp/oltr_performance_%s_%s_%s.png'
    % (num_iterations, num_train_queries, num_test_queries))
  print('running time: ', timeit.default_timer() - start)
