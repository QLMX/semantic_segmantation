#!/usr/bin/env python
# encoding: utf-8
"""
@version: python3.6
@author: QLMX
@contact: wenruichn@gmail.com
@time: 18-10-17 下午8:59
"""
import tensorflow as tf
import os, sys
import datetime, time
import multiprocessing
import numpy as np
import cv2

sys.path.append("utils")
from dataLoader import DataLoader
from tools import buildNetwork
from dataset import resizeImage
from utils import compute_class_weights, LOG, reverse_one_hot, colour_code_segmentation
from utils import evaluate_segmentation, filepath_to_name, one_hot_it


class NetWork(object):

    def __init__(self, lr, model, label_values=[], name_string=None, name_list=None, height=384, width=384,
                 is_training=False, class_balancing = False, num_val=10):

        self.starter_learning_rate = lr
        self.model = model
        self.label_values = label_values
        self.name_string = name_string
        self.name_list = name_list
        self.is_training = is_training
        self.height = height
        self.width = width
        self.class_balancing = class_balancing
        self.num_classes = len(label_values)
        self.num_val = num_val


        self.output_types = (tf.string, tf.int32, tf.float32, tf.float32)
        self.output_shapes = (tf.TensorShape([None]),
                              tf.TensorShape([None, 2]),
                              tf.TensorShape([None, self.height, self.width, 3]),
                              tf.TensorShape([None, self.height, self.width, self.num_classes]))
        self._build_model()

    def _build_input(self):

        self.it = tf.data.Iterator.from_structure(self.output_types,
                                                  self.output_shapes)
        self.path, self.size, self.img, self.mask = self.it.get_next()



    def _build_solver(self):
        self.global_step = tf.Variable(0, trainable=False)
        starter_learning_rate = self.starter_learning_rate
        learning_rate = tf.train.exponential_decay(starter_learning_rate, self.global_step,
                                                   2000, 0.9, staircase=True)

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            self.train_op = tf.train.AdamOptimizer(learning_rate).minimize(self.loss,
                                                                           global_step=self.global_step)

    def _build_model(self):

        self._build_input()

        self.logits, self.init_fn = buildNetwork(self.model, self.img, self.num_classes)

        with tf.variable_scope('loss'):
            if self.class_balancing:
                print("Computing class weights for trainlabel ...")
                class_weights = compute_class_weights(labels_dir=self.path, label_values=self.label_values)
                weights = tf.reduce_sum(class_weights * self.mask, axis=-1)
                unweighted_loss = None
                unweighted_loss = tf.nn.softmax_cross_entropy_with_logits_v2(logits=self.logits, labels=self.mask)
                loss = unweighted_loss * class_weights
            else:
                loss = tf.nn.softmax_cross_entropy_with_logits_v2(logits=self.logits, labels=self.mask)
            self.loss = tf.reduce_mean(loss)

        self._build_solver()
        self._build_summary()

    def _build_summary(self):
        tf.summary.tensor_summary("loss", self.loss)
        tf.summary.tensor_summary("learning_rate", self.starter_learning_rate)

        tf.summary.image("image", self.img)

        # tf.summary.image("ground true", tf_mask)
        # tf.summary.image("predicted", self.vis_image)

        self.merged = tf.summary.merge_all()

    def _build_data(self, data_dir='train_dir', mode='train'):
        loader = DataLoader(data_dir=data_dir, mode=mode, height=self.height, width=self.width,
                            label_value=self.label_values)
        dataset = tf.data.Dataset.from_generator(generator=loader.generator,
                                                 output_types=(tf.string,
                                                               tf.int32,
                                                               tf.float32,
                                                               tf.float32),
                                                 output_shapes=(tf.TensorShape([]),
                                                                tf.TensorShape([2]),
                                                                tf.TensorShape([self.height, self.width, 3]),
                                                                tf.TensorShape([self.height, self.width, self.num_classes])))
        return dataset

    def _bulid_save_path(self):
        now = datetime.datetime.now()
        self.save_dir = '../checkpoints/checkpoint/{}/{}_{}/'.format(self.model, now.month, now.day)
        if not os.path.exists(self.save_dir):
            os.makedirs(self.save_dir)
        self.save_dir += '/automatting.ckpt'

        self.summary_dir = '../checkpoints/summary/{}/{}_{}'.format(self.model, now.month, now.day)
        if not os.path.exists(self.summary_dir):
            os.makedirs(self.summary_dir)

        # save validation data
        self.val_dir = '../checkpoints/val/{}/{}_{}'.format(self.model, now.month, now.day)
        if not os.path.exists(self.val_dir):
            os.makedirs(self.val_dir)

    def train(self, max_epochs=20, model_dir=None, train_dir='train_set', val_dir='val_set',
              threshold=0.5, batch_size=4, write_summary=False, freq_summary=200):

        #load train data
        dataset = self._build_data(train_dir, 'train')
        dataset = dataset.shuffle(100)
        dataset = dataset.batch(batch_size)
        dataset = dataset.prefetch(20)
        train_init = self.it.make_initializer(dataset)


        #load val data
        valset = self._build_data(val_dir, 'val')
        valset = valset.batch(1)
        valset = valset.prefetch(10)
        val_init = self.it.make_initializer(valset)

        print("training starts.")
        self._bulid_save_path()

        # variables_to_restore = slim.get_model_variables()
        # for var in variables_to_restore:
        #     print(var.op.name)
        # variables_to_restore = {name_in_checkpoint(var): var for var in variables_to_restore}

        # restorer = tf.train.Saver(variables_to_restore)
        saver = tf.train.Saver()
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True

        with tf.Session(config=config) as sess:
            train_writer = tf.summary.FileWriter(self.summary_dir, sess.graph)
            # continue training
            if model_dir:
                print("continue training from " + model_dir)
                saver.restore(sess, model_dir)
            else:
                sess.run(tf.global_variables_initializer())
                # restorer.restore(sess, 'model/resnet_v1_50.ckpt')
            # train
            for epoch in range(max_epochs):
                cnt = 0
                sess.run(train_init)
                st = time.time()
                print("epoch {} begins:".format(epoch))
                try:
                    while True:
                        if write_summary:
                            _, loss, summary, step = sess.run([self.train_op,
                                                               self.loss,
                                                               self.merged,
                                                               self.global_step])

                            # evaluator.evaluate(pred_heatmap, label, img_path)
                            if step % freq_summary == 0:
                                # summary
                                train_writer.add_summary(summary, step)
                        else:
                            _, loss, step = sess.run([self.train_op, self.loss, self.global_step])
                        cnt += batch_size
                        if cnt % 20 == 0:
                            string_print = "Epoch = %d Count = %d Current_Loss = %.4f Time = %.2f" % ( epoch, cnt, loss, time.time() - st)
                            LOG(string_print)
                            st = time.time()

                except tf.errors.OutOfRangeError:
                    print('saving checkpoint......')
                    saver.save(sess, self.save_dir)
                    print('checkpoint saved.')
                    self.val_out(sess=sess, val_init=val_init, threshold=threshold, output_dir=self.val_dir, epoch=epoch)


    def val_out(self, sess, val_init, threshold=0.5, output_dir="val", epoch=0):
        print("validation starts.")
        save_dir = output_dir + "/%4d/"%epoch
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
        target = open(save_dir + "val_scores.csv", 'w')
        target.write("val_name, avg_accuracy, precision, recall, f1 score, mean iou, %s\n" % (self.name_string))

        sess.run(val_init)
        #for ind in range(self.num_val):
        scores_list = []
        class_scores_list = []
        precision_list = []
        recall_list = []
        f1_list = []
        iou_list = []
        try:
            while True:
                img, ann, output_image = sess.run([self.img, self.mask, self.logits])

                ann = np.array(ann[0, :, :, :])
                ann = reverse_one_hot(ann)

                path, size = sess.run([self.path, self.size])
                size = (size[0][0], size[0][1])

                output_image = np.array(output_image)
                output_image = np.array(output_image[0, :, :, :])
                output_image = reverse_one_hot(output_image)
                out_vis_image = colour_code_segmentation(output_image, self.label_values)

                accuracy, class_accuracies, prec, rec, f1, iou = evaluate_segmentation(pred=output_image, label=ann,
                                                                                       num_classes=self.num_classes)

                file_name = filepath_to_name(path[0])
                target.write("%s, %f, %f, %f, %f, %f" % (file_name, accuracy, prec, rec, f1, iou))
                for item in class_accuracies:
                    target.write(", %f" % (item))
                target.write("\n")

                mask = colour_code_segmentation(ann, self.label_values)

                mask = cv2.cvtColor(np.uint8(mask), cv2.COLOR_RGB2BGR)
                out_vis_image = cv2.cvtColor(np.uint8(out_vis_image), cv2.COLOR_RGB2BGR)
                mask, ori_out_vis = resizeImage(mask, out_vis_image, size)

                out_vis_image = cv2.resize(ori_out_vis[:, :, 1], size, interpolation=cv2.INTER_NEAREST)
                out_vis_image[out_vis_image < threshold * 255] = 0
                out_vis_image[out_vis_image >= threshold * 255] = 255

                img = img[0, :, :, :] * 255
                save_ori_img = cv2.cvtColor(np.uint8(img), cv2.COLOR_RGB2BGR)
                save_ori_img = cv2.resize(save_ori_img, size, interpolation=cv2.INTER_NEAREST)
                transparent_image = np.append(np.array(save_ori_img)[:, :, 0:3], out_vis_image[:, :, None], axis=-1)
                # transparent_image = Image.fromarray(transparent_image)

                cv2.imwrite(save_dir + "%s_img.jpg" % file_name, save_ori_img)
                cv2.imwrite(save_dir + "%s_ann.png" % file_name, mask)
                cv2.imwrite(save_dir + "%s_ori_pred.png" % file_name, ori_out_vis)
                cv2.imwrite(save_dir + "%s_filter_pred.png" % file_name, out_vis_image)
                cv2.imwrite(save_dir + "%s_mat.png" % file_name, transparent_image)

                scores_list.append(accuracy)
                class_scores_list.append(class_accuracies)
                precision_list.append(prec)
                recall_list.append(rec)
                f1_list.append(f1)
                iou_list.append(iou)
        except tf.errors.OutOfRangeError:
            avg_score = np.mean(scores_list)
            class_avg_scores = np.mean(class_scores_list, axis=0)
            avg_precision = np.mean(precision_list)
            avg_recall = np.mean(recall_list)
            avg_f1 = np.mean(f1_list)
            avg_iou = np.mean(iou_list)

            print("\nAverage validation accuracy for epoch # %04d = %f" % (epoch, avg_score))
            print("Average per class validation accuracies for epoch # %04d:" % (epoch))
            for index, item in enumerate(class_avg_scores):
                print("%s = %f" % (self.name_list[index], item))
            print("Validation precision = ", avg_precision)
            print("Validation recall = ", avg_recall)
            print("Validation F1 score = ", avg_f1)
            print("Validation IoU score = ", avg_iou)


    def test(self, data_dir='test_b', model_dir=None, output_dir=None):
        print("testing starts.")
        test_loader = DataLoader(data_dir=data_dir, mode='test')
        testset = tf.data.Dataset.from_generator(generator=test_loader.generator,
                                                 output_types=(tf.string,
                                                               tf.float32,
                                                               tf.float32),
                                                 output_shapes=(tf.TensorShape([]),
                                                                tf.TensorShape([512, 512, 3]),
                                                                tf.TensorShape([512, 512, 24])))
        testset = testset.batch(2)
        testset = testset.prefetch(10)
        test_init = self.it.make_initializer(testset)

        saver = tf.train.Saver()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        with tf.Session(config=config) as sess:
            saver.restore(sess, model_dir)
            sess.run(test_init)
            queue = multiprocessing.Queue(maxsize=30)
            writer_process = multiprocessing.Process(target=writer, args=[output_dir, queue, 'stop'])
            writer_process.start()
            print('writing predictions...')
            try:
                while True:
                    img_path, heatmaps = sess.run([self.img_path, self.pred_heatmap2])
                    queue.put(('continue', img_path, heatmaps))

            except tf.errors.OutOfRangeError:
                queue.put(('stop', None, None))

        writer_process.join()
        print('testing finished.')