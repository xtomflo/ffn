# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Classes for FFN model definition."""

import dataclasses

import numpy as np
import tensorflow.compat.v1 as tf

from . import optimizer


@dataclasses.dataclass
class ModelInfo:
  """Basic geometric information about the network.

  Arrays are (x, y, z), even for 2D models, in which case the z value is
  ignored.
  """
  # How far to move the field of view in the respective directions.
  deltas: np.ndarray

  # Size of the predicted patch as returned by the model.
  pred_mask_size: np.ndarray

  # Size of the input image and seed subvolumes to be used during inference.
  # This is enough information to execute a single prediction step, without
  # moving the field of view.
  input_seed_size: np.ndarray
  input_image_size: np.ndarray

  # For JAX models only: whether the predicted seed should be added to
  # its initial state.
  additive: bool = False


class FFNModel:
  """Base class for FFN models."""

  info: ModelInfo

  # Dimensionality of the model (2 or 3).
  dim: int = None

  # TF op to compute loss optimized during training. This should include all
  # loss components in case more than just the pixelwise loss is used.
  loss = None

  # TF op to call to perform loss optimization on the model.
  train_op = None

  def __init__(self,
               info: ModelInfo,
               batch_size=None,
               define_global_step=True):
    assert self.dim is not None

    self.info = info
    self.batch_size = batch_size

    # Initialize the shift collection. This is used during training with the
    # fixed step size policy.
    self.shifts = []
    for dx in (-self.info.deltas[0], 0, self.info.deltas[0]):
      for dy in (-self.info.deltas[1], 0, self.info.deltas[1]):
        for dz in (-self.info.deltas[2], 0, self.info.deltas[2]):
          if dx == 0 and dy == 0 and dz == 0:
            continue
          self.shifts.append((dx, dy, dz))

    if define_global_step:
      self.global_step = tf.compat.v1.train.get_or_create_global_step()
      #self.global_step = tf.Variable(0, name='global_step', trainable=False, dtype=tf.int32)
      
    # The seed is always a placeholder which is fed externally from the
    # training/inference drivers.
    self.input_seed = tf.placeholder(tf.float32, name='seed')
    self.input_patches = tf.placeholder(tf.float32, name='patches')

    # For training, labels should be defined as a TF object.
    self.labels = None

    # Optional. Provides per-pixel weights with which the loss is multiplied.
    # If specified, should have the same shape as self.labels.
    self.loss_weights = None

    # Updated part of the seed, in logit form.
    self.logits: tf.Operation = None

    # List of image tensors to save in summaries. The images are concatenated
    # along the X axis.
    self._images = []

  def set_input_shapes(self):
    """Sets the shape inference for input_seed and input_patches.

    Assumes input_seed_size and input_image_size are already set.
    """
    self.input_seed.set_shape([self.batch_size] +
                              list(self.info.input_seed_size[::-1]) + [1])
    self.input_patches.set_shape([self.batch_size] +
                                 list(self.info.input_image_size[::-1]) + [1])

  def set_up_sigmoid_pixelwise_loss(self, logits):
    """Sets up the loss function of the model."""
    assert self.labels is not None
    assert self.loss_weights is not None

    pixel_loss = tf.nn.sigmoid_cross_entropy_with_logits(
        logits=logits, labels=self.labels)
    pixel_loss *= self.loss_weights
    self.loss = tf.reduce_mean(pixel_loss)
    tf.summary.scalar('pixel_loss', self.loss)
    self.loss = tf.verify_tensor_all_finite(self.loss, 'Invalid loss detected')

  def set_up_optimizer(self, loss=None, max_gradient_entry_mag=0.7):
    """Sets up the training op for the model."""
    if loss is None:
      loss = self.loss
    tf.summary.scalar('optimizer_loss', self.loss)

    opt = optimizer.optimizer_from_flags()
    self.opt = opt

    grads_and_vars = opt.compute_gradients(loss)

    for g, v in grads_and_vars:
      if g is None:
        tf.logging.error('Gradient is None: %s', v.op.name)

    if max_gradient_entry_mag > 0.0:
      grads_and_vars = [(tf.clip_by_value(g, -max_gradient_entry_mag,
                                          +max_gradient_entry_mag), v)
                        for g, v, in grads_and_vars]

    trainables = tf.trainable_variables()
    if trainables:
      for var in trainables:
        tf.summary.histogram(var.name.replace(':0', ''), var)
    for grad, var in grads_and_vars:
      tf.summary.histogram('gradients/%s' % var.name.replace(':0', ''), grad)

    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
      self.train_op = opt.apply_gradients(
          grads_and_vars, global_step=self.global_step, name='train')

  def show_center_slice(self, image, sigmoid=True):
    image = image[:, image.get_shape().dims[1] // 2, :, :, :]
    if sigmoid:
      image = tf.sigmoid(image)
    self._images.append(image)

  def add_summaries(self):
    pass

  def update_seed(self, seed, update):
    """Updates the initial 'seed' with 'update'."""
    dx = self.info.input_seed_size[0] - self.info.pred_mask_size[0]
    dy = self.info.input_seed_size[1] - self.info.pred_mask_size[1]
    dz = self.info.input_seed_size[2] - self.info.pred_mask_size[2]

    if dx == 0 and dy == 0 and dz == 0:
      seed += update
    else:
      seed += tf.pad(update,
                     [[0, 0],  #
                      [dz // 2, dz - dz // 2],  #
                      [dy // 2, dy - dy // 2],  #
                      [dx // 2, dx - dx // 2],  #
                      [0, 0]])
    return seed

  def define_tf_graph(self):
    """Creates the TensorFlow graph representing the model.

    If self.labels is not None, the graph should include operations for
    computing and optimizing the loss.
    """
    raise NotImplementedError(
        'DefineTFGraph needs to be defined by a subclass.')
