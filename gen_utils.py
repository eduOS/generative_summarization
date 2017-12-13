# -*- coding: utf-8 -*-

from __future__ import unicode_literals, print_function
from __future__ import absolute_import
from __future__ import division
import tensorflow as tf
import math
import datetime
import utils
from os.path import join as join_path
from tensorflow.python import pywrap_tensorflow


def convert_to_coverage_model():
    """Load non-coverage checkpoint, add initialized extra variables for
    coverage, and save as new checkpoint"""
    print("converting non-coverage model to coverage model..")

    # initialize an entire coverage model from scratch
    sess = tf.Session(config=utils.get_config())
    print("initializing everything...")
    sess.run(tf.global_variables_initializer())

    # load all non-coverage weights from checkpoint
    saver = tf.train.Saver([v for v in tf.global_variables() if "coverage" not in v.name and "Adagrad" not in v.name])
    print("restoring non-coverage variables...")
    curr_ckpt = utils.load_ckpt(saver, sess)
    print("restored.")

    # save this model and quit
    new_fname = curr_ckpt + '_cov_init'
    print("saving model to %s..." % (new_fname))
    new_saver = tf.train.Saver()
    # this one will save all variables that now exist
    new_saver.save(sess, new_fname)
    print("saved.")
    exit()


def calc_running_avg_loss(loss, running_avg_loss, step, decay=0.9):
    """Calculate the running average loss via exponential decay.
    This is used to implement early stopping w.r.t. a more smooth loss curve than the raw loss curve.

    Args:
        loss: loss on the most recent eval step
        running_avg_loss: running_avg_loss so far
        step: training iteration step
        decay: rate of exponential decay, a float between 0 and 1. Larger is smoother.

    Returns:
        running_avg_loss: new running average loss
    """
    if running_avg_loss == 0:  # on the first iteration just take the loss
        running_avg_loss = loss
    else:
        running_avg_loss = running_avg_loss * decay + (1 - decay) * loss
    running_avg_loss = min(running_avg_loss, 12)  # clip
    loss_sum = tf.Summary()
    tag_name = 'running_avg_loss/decay=%f' % (decay)
    loss_sum.value.add(tag=tag_name, simple_value=running_avg_loss)
    tf.logging.info('running_avg_loss: %f', running_avg_loss)
    return running_avg_loss


def get_best_loss_from_chpt(val_dir):
    ckpt = tf.train.get_checkpoint_state(val_dir, "checkpoint_best")
    best_loss = None
    if ckpt:
        reader = pywrap_tensorflow.NewCheckpointReader(ckpt.model_checkpoint_path)
        var_to_shape_map = reader.get_variable_to_shape_map()
        best_loss = reader.get_tensor(
            [key for key in var_to_shape_map if "least_val_loss" in key][0]).item()
        print("the stored best loss is %s" % best_loss)
    return best_loss


def save_best_ckpt(sess, model, best_loss, val_batcher,
                   val_dir, val_saver, step, model_name='bestmodel', latest_filename="checkpoint_best"):
    bestmodel_save_path = join_path(val_dir, model_name)
    losses = []
    while True:
        val_batch = val_batcher.next_batch()
        if not val_batch:
            break
        results_val = model.run_one_step(
            sess, val_batch, update=False)
        loss_eval = results_val["loss"]
        # why there exists nan?
        if not math.isnan(loss_eval):
            losses.append(loss_eval)
    eval_loss = sum(losses) / len(losses)
    if best_loss is None or eval_loss < best_loss:
        sess.run(model.least_val_loss.assign(eval_loss))
        print(
            'Found new best model with %.3f running_avg_loss. Saving to %s %s' %
            (eval_loss, bestmodel_save_path,
                datetime.datetime.now().strftime("on %m-%d at %H:%M")))
        val_saver.save(sess, bestmodel_save_path, global_step=step, latest_filename=latest_filename)
        best_loss = eval_loss
    return eval_loss


def print_dashboard(step, batch_size, vocab_size,
                    running_avg_loss, eval_loss,
                    total_training_time, current_speed,
                    coverage_loss="not set"):
    print(
        "\nDashboard updated %s, finished steps:\t%s\n"
        "\tBatch size:\t%s\n"
        "\tVocabulary size:\t%s\n"
        "\tArticles trained:\t%s\n"
        "\tTotal training time approxiately:\t%.4f hours\n"
        "\tCurrent speed:\t%.4f seconds/article\n"
        "\tTraining loss:\t%.4f; eval loss \t%.4f"
        "\tand coverage loss:\t%s\n" % (
            datetime.datetime.now().strftime("on %m-%d at %H:%M"),
            step,
            batch_size,
            vocab_size,
            batch_size * step,
            total_training_time,
            current_speed,
            running_avg_loss, eval_loss,
            coverage_loss,
            )
    )
