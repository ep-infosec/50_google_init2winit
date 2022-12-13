# coding=utf-8
# Copyright 2022 The init2winit Authors.
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

"""Tests for losses.py.

"""
import functools
import types

from absl.testing import absltest
from absl.testing import parameterized
from init2winit.model_lib import losses
import jax
import numpy as np

CLASSIFICATION_LOSSES = ['cross_entropy', 'bi_tempered_cross_entropy']
RECONSTRUCTION_LOSSES = [
    'sigmoid_binary_cross_entropy',
    'bi_tempered_sigmoid_binary_cross_entropy',
    'sigmoid_mean_squared_error',
]

CLASSIFICATION_TEST_DATA = [{
    'logits':
        np.array([[5, 3, 4, -3, 7], [2, 5, -5, 5, 6], [-6, -5, 8, -6, 4],
                  [15, 8, -6, 4, 2], [-7, 5, -6, 9, 0]]),
    'one_hot_targets':
        np.array([[1, 0, 0, 0, 0], [0, 0, 1, 0, 0], [0, 0, 0, 1, 0],
                  [0, 0, 0, 0, 1], [0, 1, 0, 0, 0]]),
    'weights':
        None,
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
    'cross_entropy':
        8.956906,
    'bi_tempered_cross_entropy':
        1.9120569,
}, {
    'logits':
        np.array([[4, 2, 0, -4, 5], [14, 2, -5, 10, 12], [20, -3, 7, -9, 6],
                  [5, 7, -1, 2, -8], [4, -7, 9, 0, 2]]),
    'one_hot_targets':
        np.array([[0, 1, 0, 0, 0], [0, 0, 0, 1, 0], [1, 0, 0, 0, 0],
                  [0, 0, 0, 0, 1], [0, 0, 1, 0, 0]]),
    'weights':
        np.array([2, 7, 0, 3, 0]),
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
    'cross_entropy':
        6.7589717,
    'bi_tempered_cross_entropy':
        1.7393580,
}]

RECONSTRUCTION_TEST_DATA = [{
    'logits':
        np.array([[4, -5, 8, -10], [-5, 7, 4, 11], [12, 5, 5, -9],
                  [7, -11, -4, 8]]).astype(float),
    'targets':
        np.array([[0.05, 0.02, 0.96, 0.02], [0.05, 0.001, 0.5, 0.4],
                  [0.68, 0.92, 0.12, 0.22], [0.34, 0.44, 0.29, 0.2]]),
    'weights':
        None,
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
    'sigmoid_binary_cross_entropy':
        11.996754,
    'bi_tempered_sigmoid_binary_cross_entropy':
        1.9910424,
    'sigmoid_mean_squared_error':
        1.180348,
}, {
    'logits':
        np.array([[[4, -5], [8, -10]], [[-5, 7], [4, 11]], [[12, 5], [5, -9]],
                  [[7, -11], [-4, 8]]]).astype(float),
    'targets':
        np.array([[[0.05, 0.02], [0.96, 0.02]], [[0.05, 0.001], [0.5, 0.4]],
                  [[0.68, 0.92], [0.12, 0.22]], [[0.34, 0.44], [0.29, 0.2]]]),
    'weights':
        None,
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
    'sigmoid_binary_cross_entropy':
        11.996754,
    'bi_tempered_sigmoid_binary_cross_entropy':
        1.9910425,
    'sigmoid_mean_squared_error':
        1.180348,
}, {
    'logits':
        np.array([[4, -5, 8, -10], [-5, 7, 4, 11], [12, 5, 5, -9],
                  [7, -11, -4, 8]]).astype(float),
    'targets':
        np.array([[0.05, 0.02, 0.96, 0.02], [0.05, 0.001, 0.5, 0.4],
                  [0.68, 0.92, 0.12, 0.22], [0.34, 0.44, 0.29, 0.2]]),
    'weights':
        np.array([0, 4, 0, 2]),
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
    'sigmoid_binary_cross_entropy':
        16.259,
    'bi_tempered_sigmoid_binary_cross_entropy':
        2.6654808,
    'sigmoid_mean_squared_error':
        1.5073959,
}]

CROSS_ENTROPY_TEST_DATA = [{
    'logits':
        np.array([[4, 7], [-2, 5], [8, 6], [-10, -4], [3, -5]]).astype(float),
    'targets':
        np.array([[1, 0], [0, 1], [1, 0], [1, 0], [0, 1]]),
    'weights':
        None,
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
}, {
    'logits':
        np.array([[4, 7], [-2, 5], [8, 6], [-10, -4], [3, -5]]).astype(float),
    'targets':
        np.array([[1, 0], [0, 1], [1, 0], [1, 0], [0, 1]]),
    'weights':
        np.array([2, 0, 0, 6, 1]),
    'bi_tempered_t1':
        0.9,
    'bi_tempered_t2':
        2.0,
}]

CLASSIFICATION_KEYS = [
    (loss_name, loss_name) for loss_name in CLASSIFICATION_LOSSES
]
RECONSTRUCTION_KEYS = [
    (loss_name, loss_name) for loss_name in RECONSTRUCTION_LOSSES
]


class LossesTest(parameterized.TestCase):
  """Tests for losses.py."""

  def test_loss_fn_registry(self):
    for loss_name in losses._ALL_LOSS_FUNCTIONS:  # pylint: disable=protected-access
      loss_fn = losses.get_loss_fn(loss_name)
      self.assertIsInstance(loss_fn, (types.FunctionType, functools.partial))
    with self.assertRaises(ValueError):
      losses.get_loss_fn('__test__loss__name__')

  def test_output_activation_fn_registry(self):
    activation_fn = losses.get_output_activation_fn('cross_entropy')
    self.assertEqual(activation_fn.__name__, 'softmax')
    with self.assertRaises(ValueError):
      losses.get_output_activation_fn('__test__loss__name__')

  @parameterized.named_parameters(*CLASSIFICATION_KEYS)
  def test_classification_losses(self, loss_name):
    for data in CLASSIFICATION_TEST_DATA:
      loss_fn = losses.get_loss_fn(loss_name, data['bi_tempered_t1'],
                                   data['bi_tempered_t2'])
      self.assertAlmostEqual(
          loss_fn(data['logits'], data['one_hot_targets'], data['weights']),
          data[loss_name],
          places=5)

  @parameterized.named_parameters(*RECONSTRUCTION_KEYS)
  def test_regression_losses(self, loss_name):
    for data in RECONSTRUCTION_TEST_DATA:
      loss_fn = losses.get_loss_fn(loss_name, data['bi_tempered_t1'],
                                   data['bi_tempered_t2'])
      self.assertAlmostEqual(
          loss_fn(data['logits'], data['targets'], data['weights']),
          data[loss_name],
          places=6)

  def test_cross_entropy_loss_fn(self):
    for data in CROSS_ENTROPY_TEST_DATA:
      for binary_loss_name, loss_name in [
          ('sigmoid_binary_cross_entropy', 'cross_entropy'),
          ('bi_tempered_sigmoid_binary_cross_entropy',
           'bi_tempered_cross_entropy')
      ]:
        sigmoid_binary_ce_fn = losses.get_loss_fn(binary_loss_name)
        ce_fn = losses.get_loss_fn(loss_name)
        self.assertAlmostEqual(
            sigmoid_binary_ce_fn(
                np.array([[logits[0] - logits[1]] for logits in data['logits']
                         ]),
                np.array([[targets[0]] for targets in data['targets']]),
                data['weights']),
            ce_fn(data['logits'], data['targets'], data['weights']),
            places=5)

  def test_sigmoid_cross_entropy_per_label_weights(self):
    """Tests whether per label weights mask the correct entries."""
    for binary_loss_name in [
        'sigmoid_binary_cross_entropy',
        'bi_tempered_sigmoid_binary_cross_entropy']:
      sigmoid_binary_ce_fn = losses.get_loss_fn(
          binary_loss_name, bi_tempered_t1=0.9, bi_tempered_t2=2.0)
      logits = np.arange(15).reshape(3, 5)
      targets = np.arange(15, 30).reshape(3, 5)
      targets = targets / np.max(targets)

      per_label_weights = np.array([
          [1, 1, 1, 1, 0],
          [1, 1, 1, 1, 0],
          [0, 0, 0, 0, 0],
      ])
      per_example_weights = np.array([1, 1, 0])

      # Both calls normalize by the sum of weights, which is higher in the
      # per-label case.
      self.assertAlmostEqual(
          sigmoid_binary_ce_fn(logits, targets, per_label_weights),
          sigmoid_binary_ce_fn(logits[:, :4], targets[:, :4],
                               per_example_weights) / 4)

  # optax ctc loss blank token has id = 0 by default
  @parameterized.named_parameters(
      dict(
          testcase_name='2_char_no_repeat_no_blank',
          logits=np.array([[[0.1, 0.8, 0.1], [0.1, 0.1, 0.8]]]),
          labels=np.array([[1, 2]]),
          result=1.379453),
      dict(
          testcase_name='1_char_1_blank',
          logits=np.array([[[0.2, 0.8], [0.8, 0.2]]]),
          labels=np.array([[1, 0]]),
          result=0.874976))
  def test_ctc_loss(self, logits, labels, result):
    """Tests the CTC loss computation."""
    ctc_loss = losses.get_loss_fn('ctc')
    loss_value = ctc_loss(logits, np.zeros(logits.shape[:2]), labels,
                          np.zeros(labels.shape))

    self.assertAlmostEqual(loss_value, jax.numpy.array([result]), places=6)

  @parameterized.named_parameters(
      dict(
          testcase_name='single_batch',
          logits=np.array([[0.1, 0.8, 0.1], [0.1, 0.1, 0.8]]),
          targets=np.array([[1., 2., 3.], [4., 5., 6.]]),
          result=3.166667))
  def test_weighted_mean_absolute_error(self, logits, targets, result):
    """Tests computing MAE."""
    mae = losses.get_loss_fn('mean_absolute_error')
    loss_value = mae(logits, targets)

    self.assertAlmostEqual(loss_value, jax.numpy.array([result]))

if __name__ == '__main__':
  absltest.main()
