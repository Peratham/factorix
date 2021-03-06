# Test the larcqy model: l(a(r(c),q),y)

from typing import Tuple
import numpy as np
import tensorflow as tf

from naga.shared.dictionaries import Indexer
from naga.shared.learning import learn, data_to_batches, placeholder_feeder
from factorix.scoring import multilinear, multilinear_grad
# from factorix.Dictionaries import NEW_ID
# from factorix.losses import loss_quadratic_grad
from factorix.losses import loss_quadratic_grad, total_loss_logistic
from naga.shared.tf_addons import tf_eval, tf_show


def embedding_updater_model(variables, rank,
                            n_slots,
                            init_params=None,
                            n_ents=None,
                            init_noise=0.0,
                            loss=total_loss_logistic,
                            scoring=multilinear,
                            reg=0.0):
    """
    The embedding updater model that read and answer questions
    Args:
        variables: list of variables
        rank:
        n_slots:
        init_params:
        n_ents:
        init_noise:
        loss:
        scoring:
        reg:

    Returns:

    """
    qc, yc, wc, q, y, local_voc = variables
    n_data = y.get_shape()[0].value
    # model definition
    # initialization
    if init_params is not None:
        emb0_val = init_params[0]
        if len(init_params) > 1:
            step_size = init_params[1]
        else:
            step_size = 1.0
        emb0_val += np.random.randn(n_ents, rank) * init_noise
    else:
        emb0_val = np.random.randn(n_ents, rank)
        step_size = 1.0

    emb0 = tf.Variable(np.array(emb0_val, dtype=np.float32))
    if local_voc is not None:
        emb0 = tf.gather(emb0, local_voc)

    # emb0 = tf.tile(tf.reshape(tf.Variable(np.array(emb0_val, dtype=np.float32)), (1, n_ents, rank)), (n_data, 1, 1))

    # reading and answering steps
    emb1 = reader(emb0=emb0, step_size=step_size, context=(qc, yc), weights=wc, n_slots=n_slots,
                  loss_grad=loss_quadratic_grad)
    pred = answerer(emb1, q, scoring=scoring)
    objective = loss(pred, y)
    if reg > 0:
        objective += reg * tf.nn.l2_loss(emb0)

    return objective, pred, y


def multitask_to_tuples(x: np.ndarray, y: np.ndarray, intercept=True):
    if y.ndim == 1:
        y = y.reshape((-1, 1))
    n_cat = y.shape[1]
    data = []
    for i in range(x.shape[0]):
        if intercept:
            inputs = [((0, n_cat + 1), 1.0)] + [((0, j + n_cat + 2), x[i, j]) for j in range(x.shape[1])]
        else:
            inputs = [((0, j + n_cat + 1), x[i, j]) for j in range(x.shape[1])]
        outputs = [((0, cat + 1), y[i, cat]) for cat in range(y.shape[1])]
        data.append((inputs, outputs))
    return data


# class LogisticRegressionEmbeddingUpdater(EmbeddingUpdater):
#     def __init__(self, rank, n_ents, reg, n_slots=1, max_epochs=500, verbose=True, preprocessing=None):
#

class EmbeddingUpdater(object):
    def __init__(self, rank, n_ents, reg, n_slots=1, max_epochs=500, verbose=True, preprocessing=None):
        self.verbose = verbose
        self.rank = rank
        self.n_ents = n_ents
        self.n_slots = n_slots
        self.max_epochs = max_epochs
        self.reg = reg*0.001
        self.params = None
        self.preprocessing = preprocessing

    def logistic2embeddings(self, coefs, intercept=0.0):
        self.params = [np.array([[0.0, 1.0, intercept] + coefs.ravel().tolist()], dtype='float32').T]

    def fit(self, data_train, *args):
        if self.preprocessing:
            data_train = self.preprocessing(data_train, *args)
        with tf.Graph().as_default() as _:
            # create sampler and variables
            variables, sampler = machine_reading_sampler(data_train, batch_size=None)
            # main graph
            objective, _, _ = embedding_updater_model(variables, rank=self.rank,
                                                      n_ents=self.n_ents, n_slots=self.n_slots, reg=self.reg)
            # tf_debug_gradient(emb0, objective, verbose=False)  # This creates new variables...
            # train the model
            optimizer = tf.train.AdamOptimizer(learning_rate=0.1)
            hooks = []
            if self.verbose:
                hooks += [lambda it, e, xy, f: it and ((it % 100) == 0 or it is 1) and print("%d) loss=%f" % (it, f[0]))]
            self.params = learn(objective, sampler, optimizer=optimizer, hooks=hooks, max_epochs=self.max_epochs)

    def predict(self, data, *args):
        if self.preprocessing:
            data = self.preprocessing(data, *args)
        with tf.Graph().as_default() as _:
            variables_test, sampler_test = machine_reading_sampler(data, batch_size=None, shuffling=False)
            ops = embedding_updater_model(variables_test, rank=self.rank, n_ents=self.n_ents, n_slots=self.n_slots,
                                          init_params=self.params)
            nll, pred, y = tf_eval(ops)
        return pred, y, nll

    @property
    def coef_(self):
        return self.params[0][0] * self.params[0][2:]

    @property
    def intercept_(self):
        return self.params[0][0] * self.params[0][1]


def force_list_length(l, n):
    if len(l) > n:
        return l[0:n]
    elif len(l) < n:
        return l + [l[0] for _ in range(n - len(l))]
    else:
        return l


def local_vocabulary(tuples, voc):
    """
    Create a local index on a list of tuples and updates the global vocabulary
    Args:
        tuples: valued tuples, i.e. set of (t, v) pairs where t is a tuple of strings and v is the corresponding value
        voc: global vocabulary: Indexer object that stores all the strings it has seen so far

    Returns:
        triplet: (indexed_tuples, ref_to_global, new_vocabulary)
        indexed_tuples is a set of valued tuples where strings are replaced by local indices
        ref_to_global is a set of indices in the global vocabulary that are referenced by the tuples indices
        global_vocabulary is the updated global vocabulary

    Examples:
        >>> x = [(("Alice", "likes", "Bob"), True), (("Bob", "likes", "Carla"), True), (("Bob", "likes", "Alice"), False)]
        >>> y = [(("Alice", "likes", "Carla"), True), (("Alice", "sings"), True), (("Bob", "is", "alive"), False)]
        >>> voc = Indexer(("likes", "is", "work", "home", "has", "seen", "the", "have", "people"))
        >>> tuples, idx, voc = local_vocabulary(x, voc)
        >>> tuples
        [((0, 1, 2), True), ((2, 1, 3), True), ((2, 1, 0), False)]
        >>> idx
        [9, 0, 10, 11]
        >>> voc
        Indexer(likes, is, work, home, has, seen, the, have, people, Alice, Bob, Carla)
        >>> tuples, idx, voc = local_vocabulary(y, voc)
        >>> tuples
        [((0, 1, 2), True), ((0, 3), True), ((4, 5, 6), False)]
        >>> idx
        [9, 0, 11, 12, 10, 1, 13]
        >>> voc
        Indexer(likes, is, work, home, has, seen, the, have, people, Alice, Bob, Carla, sings, alive)
        >>> tuples, idx, voc = local_vocabulary(x + y, voc)
        >>> tuples
        [((0, 1, 2), True), ((2, 1, 3), True), ((2, 1, 0), False), ((0, 1, 3), True), ((0, 4), True), ((2, 5, 6), False)]
        >>> idx
        [9, 0, 10, 11, 12, 1, 13]
        >>> voc
        Indexer(likes, is, work, home, has, seen, the, have, people, Alice, Bob, Carla, sings, alive)
    """
    new_tuples = []
    local_voc0 = Indexer()
    for t, v in tuples:
        new_t = tuple([local_voc0.string_to_int(w) for w in t])
        new_tuples.append((new_t, v))
    local_voc = []
    for w in local_voc0.index_to_string:
        local_voc.append(voc(w))
    return new_tuples, local_voc, voc


def create_local_voc(data, global_voc=None):
    global_voc = global_voc or Indexer()
    new_data = []
    for inputs, outputs in data:
        seq, local_voc, global_voc = local_vocabulary(inputs + outputs, global_voc)
        new_inputs = seq[:len(inputs)]
        new_outputs = seq[len(inputs):]
        new_data.append((new_inputs, new_outputs, local_voc))
    return new_data, global_voc




def machine_reading_sampler(data, batch_size=None, n_ents=None, shuffling=True, local_voc=False):
    if local_voc:
        data, ref_to_global = create_local_voc(data)
    else:
        ref_to_global = None

    data_arr = vectorize_samples(data)
    if batch_size is not None:
        batches = data_to_batches(data_arr, batch_size, dtypes=[np.int64, np.float32, np.float32, np.int64, np.float32],
                                  shuffling=shuffling)
        qc = tf.placeholder(np.int64, (batch_size, n_ents, 2), name='question_in_context')
        yc = tf.placeholder(np.float32, (batch_size, n_ents), name='answer_in_context')
        wc = tf.placeholder(np.float32, (batch_size, n_ents), name='answer_in_context')
        q = tf.placeholder(np.int64, (batch_size, 1, 2), name='question')
        y = tf.placeholder(np.float32, (batch_size, 1), name='answer')
        if local_voc:
            raise NotImplementedError('local vocabulary to be implemented')
        sampler = placeholder_feeder((qc, yc, wc, q, y), batches)
        return (qc, yc, wc, q, y), sampler
    else:
        batches = data_to_batches(data_arr, len(data), dtypes=[np.int64, np.float32, np.float32, np.int64, np.float32],
                                  shuffling=shuffling)
        qc0, yc0, wc0, q0, y0 = [x for x in batches][0]
        qc = tf.Variable(qc0, trainable=False)
        yc = tf.Variable(yc0, trainable=False)
        wc = tf.Variable(wc0, trainable=False)
        q = tf.Variable(q0, trainable=False)
        y = tf.Variable(y0, trainable=False)
        if local_voc:
            raise NotImplementedError('local vocabulary to be implemented')
        return (qc, yc, wc, q, y, ref_to_global), None


def vectorize_samples(data, max_context_length=None):
    if max_context_length is None:
        max_context_length = np.max([len(d[0]) for d in data])
    arr = []
    for d in data:
        c, qa = d
        l = min(len(c), max_context_length)
        qc_data = np.array(force_list_length([[idx for idx in ex[0][0:l]] for ex in c], max_context_length))
        yc_data = np.array([ex[1] for ex in c[0:l]] + [0.0 for _ in range(max_context_length - l)])
        wc_data = np.array([1.0 for _ in c[0:l]] + [0.0 for _ in range(max_context_length - l)])
        q_data = np.array([[idx for idx in ex[0]] for ex in qa])
        y_data = np.array([ex[1] for ex in qa])
        arr.append((qc_data, yc_data, wc_data, q_data, y_data))
    return arr


def reader(context: Tuple[tf.Variable, tf.Variable], emb0: tf.Variable, n_slots: None,
           weights=None,
           step_size=1.0,
           scale_prediction=0.0,
           start_from_zeros=False,
           loss_grad=loss_quadratic_grad,
           emb_update=multilinear_grad):
    """
    Read a series of data and update the embeddings accordingly
    Args:
        context (Tuple[tf.Variable, tf.Variable]): contextual information
        emb0 (tf.Variable): initial embeddings
        n_slots (int): number of slots to update
        weights: weights give to every observation in the inputs. Size: (batch_size, n_obs)
        loss_grad: gradient of the loss
        emb_update: update of the embeddings (could be the gradient of the score with respect to the embeddings)

    Returns:
        The variable representing updated embeddings
    """
    if context is None:  # empty contexts are not read
        return emb0

    context_inputs, context_ouputs = context  # context_inputs has shape (n_data, n_obs, order)
    n_data, n_obs, order = [d.value for d in context_inputs.get_shape()]
    step_size = tf.Variable(step_size, name='step_size', trainable=True)

    if len(emb0.get_shape()) > 2:  # different set of embeddings for every data
        n_data2, n_ent, rank = [d.value for d in emb0.get_shape()]
        if n_slots is None:
            n_slots = n_ent
        shift_indices = tf.constant(
                n_ent * np.reshape(np.outer(range(n_data), np.ones(n_obs * order)), (n_data, n_obs, order)),
                dtype='int64')
        emb0_rsh = tf.reshape(emb0, (-1, rank))
        grad_score, preds = emb_update(emb0_rsh, context_inputs + shift_indices, score=True)
    else:
        rank = emb0.get_shape()[1].value
        grad_score, preds = emb_update(emb0, context_inputs, score=True)
    update_strength = tf.tile(tf.reshape(loss_grad(preds * scale_prediction, context_ouputs) * weights,
                                         (n_data, n_obs, 1, 1)), (1, 1, 2, rank))
    grad_loss = tf.reshape(grad_score, (n_data, n_obs, 2, rank)) * update_strength
    one_hot = tf.Variable(np.eye(n_slots + 1, n_slots, dtype=np.float32), trainable=False)  # last column removed
    indic_mat = tf.gather(one_hot, tf.minimum(context_inputs, n_slots))  # shape: (n_data, n_obs, order, n_slots)
    total_grad_loss = tf.reduce_sum(tf.batch_matmul(indic_mat, grad_loss, adj_x=True), 1)

    if start_from_zeros:
        return total_grad_loss * step_size  # size of the output: (n_data, n_slots, rank)
    else:
        if len(emb0.get_shape()) > 2:  # different set of embeddings for every data
            initial_slot_embs = emb0[:, :n_slots, :]
        else:
            initial_slot_embs = tf.reshape(tf.tile(emb0[:n_slots, :], (n_data, 1)), (n_data, n_slots, rank))
        return initial_slot_embs - total_grad_loss * step_size  # size of the output: (n_data, n_slots, rank)


def answerer(embeddings, tuples: tf.Variable, scoring=multilinear):
    """
    Evaluate the score of tuples with embeddings that are specific to every data sample

    Args:
        embeddings (tf.Variable): embedding tensor with shape (n_data, n_slots, rank)
        tuples: question tensor with int64 entries and shape (n_data, n_tuples, order)
        scoring: operator that is used to compute the scores

    Returns:
        scores (tf.Tensor): scores tensor with shape (n_data, n_tuples)

    """
    n_data, n_slots, rank = [d.value for d in embeddings.get_shape()]
    n_data, n_tuples, order = [d.value for d in tuples.get_shape()]

    shift_indices = tf.constant(np.reshape(
            np.outer(range(n_data), np.ones(n_tuples * n_slots)) * n_slots, (n_data, n_tuples, n_slots)), dtype='int64')
    questions_shifted = tuples + shift_indices

    preds = scoring(
            tf.reshape(embeddings, (n_data * n_slots, rank)),
            tf.reshape(questions_shifted, (n_data * n_tuples, order)))

    return tf.reshape(preds, (n_data, n_tuples))

#
# if __name__ == "__main__":
#     x = [(("Alice", "likes", "Bob"), True), (("Bob", "likes", "Carla"), True), (("Bob", "likes", "Alice"), False)]
#     y = [(("Alice", "likes", "Carla"), True), (("Alice", "sings"), True), (("Bob", "is", "alive"), False)]
#     voc = Indexer(("likes", "is", "work", "home", "has", "seen", "the", "have", "people"))
#     local_vocabulary(x, voc)
#     local_vocabulary(y, voc)
#     local_vocabulary(x + y, voc)

if __name__ == "__main__":
    import doctest
    doctest.testmod()