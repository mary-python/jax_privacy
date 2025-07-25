# coding=utf-8
# Copyright 2025 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""""Metrics utils."""
from typing import Mapping, Optional, Sequence

import chex
import jax
import jax.numpy as jnp
from jax_privacy.training import metrics
import numpy as np
from sklearn import linear_model
from sklearn import multiclass
from sklearn import neural_network
import sklearn.metrics as sklearn_metrics


# Aliasing for easy imports.
Avg = metrics.Avg
topk_accuracy = metrics.topk_accuracy


def per_class_acc(
    logits: chex.Array,
    labels: chex.Array,
) -> chex.Array:
  """Calculate accuracy without assuming labels are mutually exclusive.

  Args:
    logits: [batch size, number of classes].
    labels: [batch size, number of classes].

  Returns:
    Average accuracy by class.
  """

  probs = jax.nn.sigmoid(logits)
  predictions = probs > 0.5
  acc = (predictions == labels).astype(jnp.int32)
  mean_by_class = jnp.mean(acc, axis=0)
  return mean_by_class


class ArrayConcatenater:
  """Concatenate arrays (potentially up to a maximal length)."""

  def __init__(self, max_len: Optional[int] = None):
    self._array = []
    self._max_len = max_len
    self._current_len = 0

  def append(self, array: chex.Array) -> None:
    """Append array while satisfying max_len constraint."""
    if self._max_len is None:
      to_append = array
    elif self._current_len < self._max_len:
      to_append = array[:self._max_len-self._current_len]
    else:
      to_append = None

    if to_append is not None:
      self._array.append(np.asarray(to_append))
      self._current_len += len(to_append)

  def asarray(self) -> chex.ArrayNumpy:
    return np.concatenate(self._array)


def avg_and_per_class_auc(
    *,
    logits: chex.Array,
    labels: chex.Array,
    class_names: Sequence[str],
) -> Mapping[str, chex.Array]:
  """Calculate ROC-AUC without assuming labels are mutually exclusive.

  This function is not jittable.

  Args:
    logits: [batch size, number of classes].
    labels: [batch size, number of classes].
    class_names: Class name for each label id.

  Returns:
    ROC-AUC over all labels and per-label.
  """
  stats = {}

  # Need to undo one-hot-encoding

  probs = jax.nn.sigmoid(logits)

  stats['auc'] = sklearn_metrics.roc_auc_score(
      labels.reshape(-1), probs.reshape(-1))

  auc_macro = 0.0
  for class_ind, class_name in enumerate(class_names):
    class_auc = sklearn_metrics.roc_auc_score(labels[:, class_ind],
                                              probs[:, class_ind])
    stats[f'auc_{class_name}'] = class_auc
    auc_macro += class_auc

  stats['auc_macro'] = auc_macro/len(class_names)
  return stats  # pytype: disable=bad-return-type  # numpy-scalars


def per_class_disparity(
    *,
    logits: chex.Array,
    labels: chex.Array,
    class_names: Sequence[str],
) -> Mapping[str, chex.Array]:
  """Calculates per class accuracy disparity.

  This function is not jittable.

  Args:
    logits: [batch size, number of classes].
    labels: [batch size, number of classes].
    class_names: Class name for each label id.

  Returns:
    Accuracy and F1 score over all labels and per-label.
  """
  stats = {}

  # Need to undo one-hot-encoding
  predictions = jnp.argmax(logits, axis=1)
  labels = jnp.argmax(labels, axis=1)
  cm = sklearn_metrics.confusion_matrix(labels, predictions)
  avg_acc = cm.diagonal().sum() / cm.sum()
  per_class_mean = (cm.astype('float') / cm.sum(axis=1)).diagonal()
  per_class_f1 = sklearn_metrics.f1_score(
      labels,
      predictions,
      labels=np.arange(len(class_names)),
      average=None)
  # classes with accuracy less than the overall accuracy will have negative
  # absolute accuracy
  min_disparity_abs = 0
  # classes with accuracy less than the overall accuracy will have rel
  # disparity greater than 1
  max_disparity_rel = 1

  for class_name, class_acc, class_f1 in zip(
      class_names, per_class_mean, per_class_f1, strict=True):

    stats[f'acc_{class_name}'] = class_acc
    stats[f'f1_{class_name}'] = class_f1
    acc_disp = class_acc - avg_acc
    if  acc_disp < min_disparity_abs:
      min_disparity_abs = acc_disp

    if avg_acc != 1:
      rel_disp = (1 - class_acc) / (1 - avg_acc)
      if rel_disp > max_disparity_rel:
        max_disparity_rel = rel_disp

  stats['min_disparity_abs'] = min_disparity_abs
  stats['max_disparity_rel'] = max_disparity_rel
  return stats  # pytype: disable=bad-return-type  # numpy-scalars


def classify_roc_auc(
    train_samples: chex.Array,
    train_labels: chex.Array,
    eval_samples: chex.Array,
    eval_labels: chex.Array,
    train_classifier: str,
) -> float:
  """Trains an MLP on the training data and evluates it on eval.

  This function is not jittable.

  Args:
    train_samples: chex.Array of examples to train on
    train_labels: chex.Array, one-hot training labels
    eval_samples: chex.Array of example features to evaluate on
    eval_labels: chex.Array, one-hot
    train_classifier: model to train; either `mlp` or `logreg`

  Returns:
    AUROC on eval.
  """

  # pre-process data
  train_samples = train_samples.reshape((train_samples.shape[0], -1))
  eval_samples = eval_samples.reshape((eval_samples.shape[0], -1))

  model_dict = {
      'logreg': linear_model.LogisticRegression(random_state=42),
      'mlp': neural_network.MLPClassifier(random_state=42, alpha=1),
  }

  # fit multi-class classifier
  mlp_classifier = multiclass.OneVsRestClassifier(model_dict[train_classifier])
  fitted_mlp_classifier = mlp_classifier.fit(train_samples, train_labels)
  label_score = fitted_mlp_classifier.predict_proba(eval_samples)

  return sklearn_metrics.roc_auc_score(
      eval_labels.reshape(-1), label_score.reshape(-1))
