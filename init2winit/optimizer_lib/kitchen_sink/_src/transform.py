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

"""Transforms."""
from typing import Any, List, NamedTuple, Optional

import chex
import jax
import jax.numpy as jnp
import optax

# pylint:disable=invalid-name
# pylint:disable=no-value-for-parameter


def _safe_int32_increment(count: chex.Numeric) -> chex.Numeric:
  """Increments int32 counter by one.

  Normally `max_int + 1` would overflow to `min_int`. This functions ensures
  that when `max_int` is reached the counter stays at `max_int`.

  Args:
    count: a counter to be incremented.

  Returns:
    A counter incremented by 1, or max_int if the maximum precision is reached.
  """
  chex.assert_type(count, jnp.int32)
  max_int32_value = jnp.iinfo(jnp.int32).max
  one = jnp.array(1, dtype=jnp.int32)
  return jnp.where(count < max_int32_value, count + one, max_int32_value)


def _canonicalize_dtype(dtype):
  """Canonicalise a dtype, skip if None."""
  if dtype is not None:
    return jax.dtypes.canonicalize_dtype(dtype)
  return dtype


def _update_moment(updates, moments, decay, order):
  """Compute the exponential moving average of the `order-th` moment."""
  return jax.tree_map(lambda g, t: (1 - decay) * (g**order) + decay * t,
                      updates, moments)


def _bias_correction(moment, decay, count):
  """Perform bias correction. This becomes a no-op as count goes to infinity."""
  beta = 1 - decay**count
  return jax.tree_map(lambda t: t / beta.astype(t.dtype), moment)


def _bias_corrected_decay(decay, count):
  """Incorporates bias correction into decay.

  Please see section 7.1 in https://arxiv.org/pdf/1804.04235.pdf for the
  derivation of the formulas below. With bias-corrected decay, we can simply
  do

  m_{t} = decay1 * m_{t-1} + (1 - decay1) * g
  v_{t} = decay2 * v_{t-1} + (1 - decay2) * g ^ 2

  without further bias correction.

  Args:
    decay: the raw decay. As t -> infinity, bias corrected decay converges to
      this value.
    count: current step, 0-based.

  Returns:
    Bias corrected decay.
  """
  t = count.astype(jnp.float32) + 1.
  return decay * (1. - jnp.power(decay, t - 1.)) / (1. - jnp.power(decay, t))


def scale_by_learning_rate(learning_rate, flip_sign=True):
  m = -1 if flip_sign else 1
  if callable(learning_rate):
    return optax.scale_by_schedule(lambda count: m * learning_rate(count))
  return optax.scale(m * learning_rate)


def nesterov(
    decay: float = 0.9,
    accumulator_dtype: Optional[Any] = None,
) -> optax.GradientTransformation:

  return optax.trace(
      decay=decay, nesterov=True, accumulator_dtype=accumulator_dtype)


def polyak_hb(
    decay: float = 0.9,
    accumulator_dtype: Optional[Any] = None,
) -> optax.GradientTransformation:

  return optax.trace(
      decay=decay, nesterov=False, accumulator_dtype=accumulator_dtype)


def first_moment_ema(
    decay: float = 0.9,
    debias: bool = False,
    accumulator_dtype: Optional[Any] = None,
) -> optax.GradientTransformation:

  return optax.ema(
      decay=decay, debias=debias, accumulator_dtype=accumulator_dtype)


def nesterovpp(
    moment_decay: float,
    update_decay: float,
) -> optax.GradientTransformation:
  """Decouples the betas of the two Nesterov steps.

  Args:
    moment_decay: the decay rate used for the first moment.
    update_decay: the decay rate used for the update step.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    return optax.TraceState(trace=jax.tree_map(jnp.zeros_like, params))

  def update_fn(updates, state, params=None):
    del params
    f_moment = lambda g, t: g + moment_decay * t
    f_update = lambda g, t: g + update_decay * t
    new_trace = jax.tree_map(f_moment, updates, state.trace)
    updates = jax.tree_map(f_update, updates, new_trace)
    return updates, optax.TraceState(trace=new_trace)

  return optax.GradientTransformation(init_fn, update_fn)


class PreconditionBySecondMomentCoordinateWiseState(NamedTuple):
  """State for the Adam preconditioner."""
  count: chex.Array
  nu: optax.Updates


def precondition_by_rms(
    decay: float = 0.999,
    eps: float = 1e-8,
    eps_root: float = 0.0,
    debias: bool = False,
    power: float = 0.5,
) -> optax.GradientTransformation:
  """Preconditions updates according to the RMS Preconditioner from Adam.

  References:
    [Kingma, Ba 2015] https://arxiv.org/pdf/1412.6980.pdf

  Args:
    decay: decay rate for exponentially weighted average of moments of grads.
    eps: Term added to the denominator to improve numerical stability. The
      default is kept to 1e-8 to match optax Adam implementation.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.
    debias: whether to use bias correction or not
    power: the power to which the second moment is raised to
  Gotcha: Note that the usage of epsilon and defaults are different from optax's
    scale_by_rms. This matches optax's adam template.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  raise_power = jnp.sqrt if power == 0.5 else lambda x: jnp.power(x, power)

  def init_fn(params):
    return PreconditionBySecondMomentCoordinateWiseState(
        count=jnp.zeros([], jnp.int32), nu=jax.tree_map(jnp.zeros_like, params))

  def update_fn(updates, state, params=None):
    del params
    nu = _update_moment(updates, state.nu, decay, 2)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    nu_hat = nu if not debias else _bias_correction(nu, decay, count)
    updates = jax.tree_map(lambda u, v: u / (raise_power(v + eps_root) + eps),
                           updates, nu_hat)
    return updates, PreconditionBySecondMomentCoordinateWiseState(
        count=count, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


def precondition_by_yogi(b2: float = 0.999,
                         eps: float = 1e-8,
                         eps_root: float = 0.0,
                         initial_accumulator_value: float = 1e-6,
                         debias: bool = True) -> optax.GradientTransformation:
  """Preconditions updates according to the Yogi Preconditioner.

  References:
    [Zaheer et al,
    2018](https://papers.nips.cc/paper/2018/hash/90365351ccc7437a1309dc64e4db32a3-Abstract.html)
    #pylint:disable=line-too-long

  Args:
    b2: decay rate for the exponentially weighted average of moments of grads.
    eps: Term added to the denominator to improve numerical stability. The
      default is changed to 1e-8. Optax Yogi's default is 1e-3.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.
    initial_accumulator_value: The starting value for accumulators. Only
      positive values are allowed.
    debias: whether to use bias correction or not

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    value_like = lambda p: jnp.full_like(p, initial_accumulator_value)
    nu = jax.tree_map(value_like, params)  # Second Central moment
    return PreconditionBySecondMomentCoordinateWiseState(
        count=jnp.zeros([], jnp.int32), nu=nu)

  def update_fn(updates, state, params=None):
    del params
    nu = jax.tree_map(lambda g, v: v - (1 - b2) * jnp.sign(v - g**2) * (g**2),
                      updates, state.nu)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    nu_hat = nu if not debias else _bias_correction(nu, b2, count)
    updates = jax.tree_map(lambda u, v: u / (jnp.sqrt(v + eps_root) + eps),
                           updates, nu_hat)
    return updates, PreconditionBySecondMomentCoordinateWiseState(
        count=count, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


# TODO(namanagarwal): Testing needed
def precondition_by_amsgrad(
    b2: float = 0.999,
    eps: float = 1e-8,
    eps_root: float = 0.0,
) -> optax.GradientTransformation:
  """Rescale updates according to the AMSGrad algorithm.

  References:
    [Reddi et al, 2018](https://arxiv.org/abs/1904.09237v1)

  Args:
    b2: decay rate for the exponentially weighted average of squared grads.
    eps: term added to the denominator to improve numerical stability.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    return PreconditionBySecondMomentCoordinateWiseState(
        count=jnp.zeros([], jnp.int32), nu=jax.tree_map(jnp.zeros_like, params))

  def update_fn(updates, state, params=None):
    del params
    nu = _update_moment(updates, state.nu, b2, 2)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    nu_hat = jax.tree_map(jnp.maximum, nu, state.nu)
    updates = jax.tree_map(lambda m, v: m / (jnp.sqrt(v + eps_root) + eps),
                           updates, nu_hat)
    return updates, PreconditionBySecondMomentCoordinateWiseState(
        count=count, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


class ScaleByAMSGradState(NamedTuple):
  """State for the AMSGrad algorithm."""
  mu: optax.Updates
  nu: optax.Updates


def scale_by_amsgrad(
    b1: float = 0.9,
    b2: float = 0.999,
    eps: float = 1e-8,
    eps_root: float = 0.0,
    mu_dtype: Optional[Any] = None,
) -> optax.GradientTransformation:
  """Rescale updates according to the AMSGrad algorithm.

  References:
    [Reddi et al, 2018](https://arxiv.org/abs/1904.09237v1)

  Args:
    b1: decay rate for the exponentially weighted average of grads.
    b2: decay rate for the exponentially weighted average of squared grads.
    eps: term added to the denominator to improve numerical stability.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.
    mu_dtype: optional `dtype` to be used for the first order accumulator; if
      `None` then the `dtype is inferred from `params` and `updates`.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    mu = jax.tree_map(  # First moment
        lambda t: jnp.zeros_like(t, dtype=mu_dtype), params)
    nu = jax.tree_map(jnp.zeros_like, params)  # Second moment
    return ScaleByAMSGradState(mu=mu, nu=nu)

  def update_fn(updates, state, params=None):
    del params
    mu = _update_moment(updates, state.mu, b1, 1)
    nu = _update_moment(updates, state.nu, b2, 2)
    nu_hat = jax.tree_map(jnp.maximum, nu, state.nu)
    updates = jax.tree_map(lambda m, v: m / (jnp.sqrt(v + eps_root) + eps), mu,
                           nu_hat)
    return updates, ScaleByAMSGradState(mu=mu, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


# TODO(namanagarwal) : Remove the following
class BiasCorrectionState(NamedTuple):
  """Holds an exponential moving average of past updates."""
  count: chex.Array  # shape=(), dtype=jnp.int32.


def bias_correction(
    decay: float = 0.9,
    accumulator_dtype: Optional[Any] = None) -> optax.GradientTransformation:
  """Compute the Adam style bias correction.

  Args:
    decay: the decay rate for the exponential moving average.
    accumulator_dtype: optional `dtype` to used for the accumulator; if `None`
      then the `dtype` is inferred from `params` and `updates`.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  accumulator_dtype = _canonicalize_dtype(accumulator_dtype)

  def init_fn(params):
    del params
    return BiasCorrectionState(count=jnp.zeros([], jnp.int32))

  def update_fn(updates, state, params=None):
    del params
    count_inc = _safe_int32_increment(state.count)
    new_vals = _bias_correction(updates, decay, count_inc)
    return new_vals, BiasCorrectionState(count=count_inc)

  return optax.GradientTransformation(init_fn, update_fn)


# TODO(namanagarwal): Add a test for Nadam
class ScaleByAdamState(NamedTuple):
  """State for the NAdam algorithm."""
  count: chex.Array  # shape=(), dtype=jnp.int32.
  mu: optax.Updates
  nu: optax.Updates


def scale_by_adam(b1: float = 0.9,
                  b2: float = 0.999,
                  eps: float = 1e-8,
                  eps_root: float = 0.0,
                  debias: bool = True,
                  power: float = 0.5) -> optax.GradientTransformation:
  """Rescale updates according to the Adam algorithm.

  Args:
    b1: decay rate for the exponentially weighted average of grads.
    b2: decay rate for the exponentially weighted average of squared grads.
    eps: term added to the denominator to improve numerical stability.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.
    debias: whether to use moment bias correction.
    power: the power to use in the preconditioner (0.5 in default adam).

  Returns:
    An (init_fn, update_fn) tuple.
  """
  raise_power = jnp.sqrt if power == 0.5 else lambda x: jnp.power(x, power)

  def init_fn(params):
    mu = jax.tree_map(jnp.zeros_like, params)  # First moment
    nu = jax.tree_map(jnp.zeros_like, params)  # Second moment
    return ScaleByAdamState(count=jnp.zeros([], jnp.int32), mu=mu, nu=nu)

  def update_fn(updates, state, params=None):
    del params

    mu = _update_moment(updates, state.mu, b1, 1)
    nu = _update_moment(updates, state.nu, b2, 2)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    mu_hat = mu if not debias else _bias_correction(mu, b1, count)
    nu_hat = nu if not debias else _bias_correction(nu, b2, count)
    updates = jax.tree_map(
        lambda m, v: m / (raise_power(v + eps_root) + eps), mu_hat, nu_hat)
    return updates, ScaleByAdamState(count=count, mu=mu, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


class PreconditionByRssState(NamedTuple):
  """State holding the sum of gradient squares to date."""
  sum_of_squares: optax.Updates


def precondition_by_rss(
    initial_accumulator_value: float = 0.1,
    eps: float = 1e-7,
    power: float = 0.5
) -> optax.GradientTransformation:
  """Rescale updates by the powers of the sum of all squared gradients to date.

  Args:
    initial_accumulator_value: starting value for accumulators, must be >= 0.
    eps: a small floating point value to avoid zero denominator.
    power: the power to use when scaling (default is 0.5 to scale by root sum
    of squares).

  Returns:
    An (init_fn, update_fn) tuple.
  """
  raise_power = jnp.sqrt if power == 0.5 else lambda x: jnp.power(x, power)

  def init_fn(params):
    sum_of_squares = jax.tree_map(
        lambda t: jnp.full_like(t, initial_accumulator_value), params)
    return PreconditionByRssState(sum_of_squares=sum_of_squares)

  def update_fn(updates, state, params=None):
    del params
    sum_of_squares = jax.tree_map(
        lambda g, t: jnp.square(g) + t, updates, state.sum_of_squares)
    updates = jax.tree_map(lambda scale, g: g / raise_power(scale + eps),
                           sum_of_squares, updates)
    return updates, PreconditionByRssState(sum_of_squares=sum_of_squares)

  return optax.GradientTransformation(init_fn, update_fn)


def _sanitize_values(array, replacement=0.0):
  """Sanitizes NaN and Infinity values."""
  return jnp.nan_to_num(
      array, nan=replacement, posinf=replacement, neginf=replacement)


def sanitize_values(replacement=0.0):
  """Sanitizes updates by replacing NaNs and Infinity values with zeros.

  Args:
    replacement: value to replace NaNs and Infinity.

  Returns:
    An (init_fn, update_fn) tuple.0
  """

  def init_fn(params):
    del params
    return

  def update_fn(updates, state, params=None):
    del params

    updates = jax.tree_map(lambda x: _sanitize_values(x, replacement), updates)
    return updates, state

  return optax.GradientTransformation(init_fn, update_fn)


def _reduce_mean(array):
  num_elements = array.size
  if num_elements > 1e8:
    # When x is too large, simple jnp.mean() can result in nan or inf values.
    array_sum = jnp.sum(array, axis=-1)
    array_sum = jnp.sum(array_sum)
    return array_sum / jnp.array(num_elements, dtype=array_sum.dtype)
  else:
    return jnp.mean(array)


def _reduce_rms(array):
  """Computes the RMS of `array` (in a numerically stable way).

  Args:
    array: Input array.

  Returns:
    The root mean square of the input array as a scalar array.
  """
  sq = jnp.square(array)
  sq_mean = _reduce_mean(sq)
  return jnp.sqrt(sq_mean)


def _clip_update(update, clip_threshold):
  mean_update = _sanitize_values(_reduce_rms(update), 1.0)
  clip_threshold = jnp.array(clip_threshold, dtype=update.dtype)
  denom = jnp.maximum(1.0, mean_update / clip_threshold)

  return update / denom


def clip_updates(clip_threshold=1.0):
  """Implemented update capping as described in adafactor paper.

  http://proceedings.mlr.press/v80/shazeer18a/shazeer18a.pdf

  Args:
    clip_threshold: upper threshold beyond which we scale updates.

  Returns:
    An (init_fn, update_fn) tuple.0
  """

  def init_fn(params):
    del params
    return

  def update_fn(updates, state, params=None):
    del params

    updates = jax.tree_map(lambda x: _clip_update(x, clip_threshold), updates)
    return updates, state

  return optax.GradientTransformation(init_fn, update_fn)


def scale_by_nadam(b1: float = 0.9,
                   b2: float = 0.999,
                   eps: float = 1e-8,
                   eps_root: float = 0.0,
                   debias: bool = True,
                   power: float = 0.5) -> optax.GradientTransformation:
  """Rescale updates according to the NAdam algorithm.

  References:
  There seem to be multiple versions of NAdam. The original version is here
  https://openreview.net/forum?id=OM0jvwB8jIp57ZJjtNEZ (the pytorch imp. also
  follows this)

  Current code implements a simpler version with no momentum decay and slightly
  different bias correction terms. The exact description can be found here
  https://arxiv.org/pdf/1910.05446.pdf (Table 1)

  Args:
    b1: decay rate for the exponentially weighted average of grads.
    b2: decay rate for the exponentially weighted average of squared grads.
    eps: term added to the denominator to improve numerical stability.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.
    debias: whether to use bias correction.
    power: the power to use in the preconditioner (0.5 in default adam).

  Returns:
    An (init_fn, update_fn) tuple.
  """
  raise_power = jnp.sqrt if power == 0.5 else lambda x: jnp.power(x, power)

  def init_fn(params):
    mu = jax.tree_map(jnp.zeros_like, params)  # First moment
    nu = jax.tree_map(jnp.zeros_like, params)  # Second moment
    return ScaleByAdamState(count=jnp.zeros([], jnp.int32), mu=mu, nu=nu)

  def update_fn(updates, state, params=None):
    del params
    mu = _update_moment(updates, state.mu, b1, 1)
    nu = _update_moment(updates, state.nu, b2, 2)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    mu_hat = _update_moment(updates, mu, b1, 1)
    mu_hat = mu_hat if not debias else _bias_correction(mu_hat, b1, count)
    nu_hat = nu if not debias else _bias_correction(nu, b2, count)
    updates = jax.tree_map(
        lambda m, v: m / (raise_power(v + eps_root) + eps), mu_hat, nu_hat)
    return updates, ScaleByAdamState(count=count, mu=mu, nu=nu)

  return optax.GradientTransformation(init_fn, update_fn)


class PreconditionByLayeredAdaptiveRMSState(NamedTuple):
  """State for the Layered Adaptive RMS Preconditioner algorithm."""
  count: chex.Array  # shape=(), dtype=jnp.int32.
  nu: optax.Updates
  beta_array: Any
  scale_array: Any


def precondition_by_layered_adaptive_rms(
    decays: List[float],
    scales: List[float],
    decay_distribution: List[float],
    modality: Any = 'output',
    eps: float = 1e-8,
    eps_root: float = 0.0,
    # debias: bool = False,
) -> optax.GradientTransformation:
  """Code implementing beta-factored rms preconditioner.

  Args:
    decays: List of beta values
    scales: List of scales accompanying the beta values. Must of the same length
      as decays.
    decay_distribution: distribution according to which tensors are divided.
      Must be of the same length as decays.
    modality: one of two values 'input' vs 'output' indicating whether factoring
      is perfomed on the input vs the output axis. For conv-nets convention fits
      the Height-Width-InpChannels-OutChannels convention of parameters in flax
      linen Conv
    eps: term added to the denominator to improve numerical stability.
    eps_root: term added to the denominator inside the square-root to improve
      numerical stability when backpropagating gradients through the rescaling.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    count = jnp.zeros([], jnp.int32)
    nu = jax.tree_map(jnp.zeros_like, params)
    beta_array = jax.tree_map(
        lambda x: _generate_per_parameter_array(decays, x), params)
    scale_array = jax.tree_map(
        lambda x: _generate_per_parameter_array(scales, x), params)

    return PreconditionByLayeredAdaptiveRMSState(
        count=count, nu=nu, beta_array=beta_array, scale_array=scale_array)

  def _generate_partition(decays, decay_distribution, length):
    # Generates length-sized array split according to decay_distribution.
    decays = jnp.array(decays)
    decay_distribution = jnp.array(decay_distribution)
    multiples = jnp.int32(jnp.floor(decay_distribution * length))
    multiples = multiples.at[-1].set(multiples[-1] + length -
                                     jnp.sum(multiples))
    return jnp.repeat(decays, multiples)

  def _generate_per_parameter_array(arr, params):
    # For 1D Tensor - input_Index = output_index = 1
    # For 2D Tensor - input_Index = 0, output_index = 1
    # For > 2D Tensor - input_Index = second last dim , output_index = last dim
    input_index = max(len(params.shape) - 2, 0)
    output_index = len(params.shape) - 1
    array_len = params.shape[
        output_index] if modality == 'output' else params.shape[input_index]
    full_array = _generate_partition(arr, decay_distribution, array_len)
    # enlarge beta array to have same ndims as params but ones in shape
    # everywhere but the target_index for easy broadcasting
    target_index = output_index if modality == 'output' else input_index
    new_shape = [1] * jnp.ndim(params)
    new_shape[target_index] = array_len
    return jnp.reshape(full_array, new_shape)

  def _update_moment_general(updates, moments, beta, order):
    # input beta can be an array here
    # the following code handles the adagrad case
    one_minus_beta = 1.0 - beta
    one_minus_beta = jnp.where(one_minus_beta <= 0.0,
                               jnp.ones_like(one_minus_beta), one_minus_beta)
    return one_minus_beta * (updates**order) + beta * moments

  def update_fn(updates, state, params=None):
    del params
    nu = jax.tree_map(lambda g, t, b: _update_moment_general(g, t, b, 2),
                      updates, state.nu, state.beta_array)
    count = state.count + jnp.array(1, dtype=jnp.int32)
    # Decide what to do with bias correction
    # nu_hat = nu if not debias else _bias_correction(nu, b2, count)
    updates = jax.tree_map(
        lambda m, v, s: s * (m / (jnp.sqrt(v + eps_root) + eps)), updates, nu,
        state.scale_array)
    return updates, PreconditionByLayeredAdaptiveRMSState(
        count=count,
        nu=nu,
        beta_array=state.beta_array,
        scale_array=state.scale_array)

  return optax.GradientTransformation(init_fn, update_fn)


class Polyak_AveragingState(NamedTuple):
  """State for Polyak Averaging."""
  ema: optax.Updates


def polyak_averaging(decay: float = 0.999) -> optax.GradientTransformation:
  """Preconditions updates according to the RMS Preconditioner from Adam.

  References:
    [Polyak, Juditsky 1992] -
    https://epubs.siam.org/doi/abs/10.1137/0330046?journalCode=sjcodc

  Args:
    decay: decay rate for exponentially weighted average of moments of params.

  Returns:
    An (init_fn, update_fn) tuple.
  """

  def init_fn(params):
    return Polyak_AveragingState(ema=jax.tree_map(jnp.zeros_like, params))

  def update_fn(updates, state, params):
    new_ema = _update_moment(params, state.ema, decay, 1)
    return updates, Polyak_AveragingState(ema=new_ema)

  return optax.GradientTransformation(init_fn, update_fn)

AddDecayedWeightsState = optax.EmptyState


def add_decayed_weights(
    weight_decay: float = 0.0,
    learning_rate: float = 1.0,
    flip_sign: float = False,
) -> optax.GradientTransformation:
  """Add parameter scaled by `weight_decay`.

  Args:
    weight_decay: A scalar weight decay rate.
    learning_rate: An optional learning rate parameter to multiplied with the
      weight decay
    flip_sign: flips the sign of the weight decay operation. Default is False
      under which weight decay is added mirroring the default behavior in optax
      add_decayed_weights. True is to be used when there is no eventual
      scale_by_learning_rate in the chain which flips the sign.

  Returns:
    A `GradientTransformation` object.
  """
  m = -1 if flip_sign else 1

  def init_fn(params):
    del params
    return AddDecayedWeightsState()

  def update_fn(updates, state, params):
    if params is None:
      raise ValueError(optax.NO_PARAMS_MSG)
    updates = jax.tree_util.tree_map(
        lambda g, p: g + m * learning_rate * weight_decay * p,
        updates, params)
    return updates, state

  return optax.GradientTransformation(init_fn, update_fn)


# scale_by_rms exists only for backward compatability
_composites = {
    'scale_by_adam': scale_by_adam,
    'scale_by_yogi': optax.scale_by_yogi,
    'scale_by_amsgrad': scale_by_amsgrad,
    'scale_by_nadam': scale_by_nadam,
    'scale_by_rms': precondition_by_rms,
    'sgd': optax.sgd,
}

_first_moment_accumulators = {
    'nesterov': nesterov,
    'polyak_hb': polyak_hb,
    'first_moment_ema': first_moment_ema,
    'nesterovpp': nesterovpp,
}

_preconditioners = {
    'precondition_by_rms': precondition_by_rms,
    'precondition_by_yogi': precondition_by_yogi,
    'precondition_by_rss': precondition_by_rss,
    'precondition_by_amsgrad': precondition_by_amsgrad,
    'precondition_by_layered_adaptive_rms': precondition_by_layered_adaptive_rms
}

_miscellaneous = {
    'scale_by_learning_rate': scale_by_learning_rate,
    'add_decayed_weights': add_decayed_weights,
    'polyak_averaging': polyak_averaging,
    'clip_updates': clip_updates,
    'sanitize_values': sanitize_values
}

transformation_registry = {}
transformation_registry.update(_composites)
transformation_registry.update(_preconditioners)
transformation_registry.update(_first_moment_accumulators)
transformation_registry.update(_miscellaneous)
