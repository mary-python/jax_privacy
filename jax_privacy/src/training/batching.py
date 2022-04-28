# coding=utf-8
# Copyright 2022 DeepMind Technologies Limited.
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

"""Virtual batching to allow easy accumulation across devices and steps.

Typical usage:
  batching = VirtualBatching(
      batch_size_init=128,
      batch_size_per_device_per_step=32,
      num_devices=2,
  )
  global_step = 0

  # At each step, the batch-size accumulated across devices is 32*2=64.
  batching.batch_size_per_step
  >>> 64
  # Thus in order to get a total batch-size of 128, we must accumulate over 2
  # steps before actually applying a model update.
  batching.apply_update_every(global_step)
  >>> 2
"""

from typing import Dict, Optional

import chex
import jax
import optax


class VirtualBatching:
  """Batching across devices and steps with a potential schedule."""

  def __init__(
      self,
      batch_size_init: int,
      batch_size_per_device_per_step: int,
      scale_schedule: Optional[Dict[int, int]] = None,
      num_devices: Optional[int] = None,
  ):
    """Init function.

    Args:
      batch_size_init: initial value for the total batch-size.
      batch_size_per_device_per_step: batch-size to fit on each device at
        every step.
      scale_schedule: schedule to adapt the total batch-size across iterations.
      num_devices: number of available devices for parallelization.
    """
    self.batch_size_init = batch_size_init
    self.batch_size_per_device_per_step = batch_size_per_device_per_step
    self.scale_schedule = scale_schedule
    self.num_devices = (
        num_devices if num_devices is not None else jax.device_count())

    if self.batch_size_init % self.batch_size_per_step:
      raise ValueError(
          f'Batch-size {self.batch_size_init} not divisible by '
          f'{self.batch_size_per_device_per_step} * {self.num_devices}'
      )

    self._batch_size_fn = optax.piecewise_constant_schedule(
        init_value=self.batch_size_init,
        boundaries_and_scales=self.scale_schedule,
    )

  def batch_size(self, global_step: chex.Numeric) -> chex.Numeric:
    """Total batch-size at a given step."""
    return self._batch_size_fn(global_step)

  @property
  def batch_size_per_step(self) -> chex.Numeric:
    """Batch-size per step (accumulated over devices) at any step."""
    return self.batch_size_per_device_per_step * self.num_devices

  def apply_update_every(self, global_step: chex.Numeric) -> chex.Numeric:
    """Number of accumulation steps required before performing an update."""
    return self.batch_size(global_step) // self.batch_size_per_step

  def data_seen(self, global_step: chex.Numeric) -> chex.Numeric:
    """Total number of data points seen from beginning until global_step."""
    return global_step * self.batch_size_per_step
