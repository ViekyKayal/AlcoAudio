# -*- coding: utf-8 -*-
"""
@created on: 7/6/20,
@author: Shreesha N,
@version: v0.0.1
@system name: badgod
Description:

..todo::

"""

from torch.utils.tensorboard import SummaryWriter
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import time
import json
import random

from alcoaudio.networks.sincnet import SincNet
from alcoaudio.utils import file_utils
from alcoaudio.utils.network_utils import accuracy_fn, log_summary, custom_confusion_matrix, \
    log_conf_matrix, write_to_npy, to_tensor, to_numpy
from alcoaudio.utils.data_utils import read_npy
from alcoaudio.datagen.augmentation_methods import librosaSpectro_to_torchTensor, time_mask, freq_mask


class SincNetRunner:
    def __init__(self, args):
        self.run_name = args.run_name + '_' + str(time.time()).split('.')[0]
        self.current_run_basepath = args.network_metrics_basepath + '/' + self.run_name + '/'
        self.learning_rate = args.learning_rate
        self.epochs = args.epochs
        self.test_net = args.test_net
        self.train_net = args.train_net
        self.batch_size = args.batch_size
        self.num_classes = args.num_classes
        self.audio_basepath = args.audio_basepath
        self.train_data_file = args.train_data_file
        self.test_data_file = args.test_data_file
        self.data_read_path = args.data_save_path
        self.is_cuda_available = torch.cuda.is_available()
        self.display_interval = args.display_interval
        self.sampling_rate = args.sampling_rate
        self.sample_size_in_seconds = args.sample_size_in_seconds
        self.overlap = args.overlap

        self.network_metrics_basepath = args.network_metrics_basepath
        self.tensorboard_summary_path = self.current_run_basepath + args.tensorboard_summary_path
        self.network_save_path = self.current_run_basepath + args.network_save_path

        self.network_restore_path = args.network_restore_path

        self.device = torch.device("cuda" if self.is_cuda_available else "cpu")
        self.network_save_interval = args.network_save_interval
        self.normalise = args.normalise_while_training
        self.dropout = args.dropout
        self.threshold = args.threshold
        self.debug_filename = self.current_run_basepath + '/' + args.debug_filename

        paths = [self.network_save_path, self.tensorboard_summary_path]
        file_utils.create_dirs(paths)

        args.sincnet_saved_model = args.data_save_path + args.sincnet_saved_model
        self.sincnet_params = []

        self.network = SincNet(args).to(self.device)
        # for x in self.network.state_dict().keys():
        #     # if isinstance(self.network.state_dict()[x], float):
        #     print(x, torch.max(self.network.state_dict()[x]), torch.min(self.network.state_dict()[x]))
        self.saved_model = \
            torch.load(args['sincnet_saved_model'], map_location=self.device)[
                'CNN_model_par']
        self.load_my_state_dict(self.saved_model)
        print(self.sincnet_params)
        # exit()
        for i, param in enumerate(self.network.parameters()):
            # if name in self.sincnet_params:
            # self.network.state_dict()[name].requires_grad = False
            if i <= 19:
                param.requires_grad = False
        # for x in self.network.state_dict().keys():
        #     # if isinstance(self.network.state_dict()[x], float):
        #     print(x, torch.max(self.network.state_dict()[x]), torch.min(self.network.state_dict()[x]))

        self.pos_weight = None
        self.loss_function = None
        self.learning_rate_decay = args.learning_rate_decay

        self.optimiser = optim.RMSprop(self.network.parameters(), lr=self.learning_rate, alpha=0.95, eps=1e-8)
        self.scheduler = torch.optim.lr_scheduler.ExponentialLR(self.optimiser, gamma=self.learning_rate_decay)

        self._min, self._max = float('inf'), -float('inf')

        if self.train_net:
            self.network.train()
            self.log_file = open(self.network_save_path + '/' + self.run_name + '.log', 'w')
            self.log_file.write(json.dumps(args))
        if self.test_net:
            print('Loading Network')
            self.network.load_state_dict(torch.load(self.network_restore_path, map_location=self.device))
            self.network.eval()
            self.log_file = open(self.network_restore_path.replace('_40.pt', '.log'), 'a')
            print('\n\n\n********************************************************', file=self.log_file)
            print('Testing Model - ', self.network_restore_path)
            print('Testing Model - ', self.network_restore_path, file=self.log_file)
            print('********************************************************', file=self.log_file)

        self.writer = SummaryWriter(self.tensorboard_summary_path)
        print("Network config:\n", self.network)
        print("Network config:\n", self.network, file=self.log_file)

        self.batch_loss, self.batch_accuracy, self.uar = [], [], []

        print('Configs used:\n', json.dumps(args, indent=4))
        print('Configs used:\n', json.dumps(args, indent=4), file=self.log_file)

    def load_my_state_dict(self, state_dict):

        own_state = self.network.state_dict()
        for name, param in state_dict.items():
            self.sincnet_params.append(name)
            if name not in own_state:
                continue
            if isinstance(param, nn.Parameter):
                # backwards compatibility for serialized parameters
                param = param.data
            own_state[name].copy_(param)

    def data_reader(self, data_filepath, label_filepath, train, should_batch=True, shuffle=True, infer=False):
        if infer:
            pass
        else:
            input_data, labels = read_npy(data_filepath), read_npy(label_filepath)
            if train:

                print('Original data size - before Augmentation')
                print('Original data size - before Augmentation', file=self.log_file)
                print('Total data ', len(input_data))
                print('Event rate', sum(labels) / len(labels))
                print(np.array(input_data).shape, np.array(labels).shape)

                print('Total data ', len(input_data), file=self.log_file)
                print('Event rate', sum(labels) / len(labels), file=self.log_file)
                print(np.array(input_data).shape, np.array(labels).shape, file=self.log_file)

                for x in input_data:
                    self._min = min(np.min(x), self._min)
                    self._max = max(np.max(x), self._max)

                data = [(x, y) for x, y in zip(input_data, labels)]
                random.shuffle(data)
                input_data, labels = np.array([x[0] for x in data]), [x[1] for x in data]

                # Initialize pos_weight based on training data
                self.pos_weight = len([x for x in labels if x == 0]) / len([x for x in labels if x == 1])
                print('Pos weight for the train data - ', self.pos_weight)
                print('Pos weight for the train data - ', self.pos_weight, file=self.log_file)

            print('Total data ', len(input_data))
            print('Event rate', sum(labels) / len(labels))
            print(np.array(input_data).shape, np.array(labels).shape)

            print('Total data ', len(input_data), file=self.log_file)
            print('Event rate', sum(labels) / len(labels), file=self.log_file)
            print(np.array(input_data).shape, np.array(labels).shape, file=self.log_file)

            print('Min max values used for normalisation ', self._min, self._max)
            print('Min max values used for normalisation ', self._min, self._max, file=self.log_file)

            # Normalizing `input data` on train dataset's min and max values
            if self.normalise:
                input_data = (input_data - self._min) / (self._max - self._min)

            if should_batch:
                batched_input = [input_data[pos:pos + self.batch_size] for pos in
                                 range(0, len(input_data), self.batch_size)]
                batched_labels = [labels[pos:pos + self.batch_size] for pos in range(0, len(labels), self.batch_size)]
                return batched_input, batched_labels
            else:
                return input_data, labels

    def run_for_epoch(self, epoch, x, y, type):
        self.network.eval()
        for m in self.network.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.track_running_stats = False
        predictions_dict = {"tp": [], "fp": [], "tn": [], "fn": []}
        predictions = []
        self.test_batch_loss, self.test_batch_accuracy, self.test_batch_uar, self.test_batch_ua, self.test_batch_f1, self.test_batch_precision, self.test_batch_recall, audio_for_tensorboard_test = [], [], [], [], [], [], [], None
        with torch.no_grad():
            for i, (audio_data, label) in enumerate(zip(x, y)):
                label = to_tensor(label, device=self.device).float()
                audio_data = to_tensor(audio_data, device=self.device)
                test_predictions = self.network(audio_data).squeeze(1)
                test_loss = self.loss_function(test_predictions, label)
                test_predictions = nn.Sigmoid()(test_predictions)
                predictions.append(to_numpy(test_predictions))
                test_accuracy, test_uar, test_precision, test_recall, test_f1 = accuracy_fn(test_predictions, label,
                                                                                            self.threshold)
                self.test_batch_loss.append(to_numpy(test_loss))
                self.test_batch_accuracy.append(to_numpy(test_accuracy))
                self.test_batch_uar.append(test_uar)
                self.test_batch_f1.append(test_f1)
                self.test_batch_precision.append(test_precision)
                self.test_batch_recall.append(test_recall)

                tp, fp, tn, fn = custom_confusion_matrix(test_predictions, label, threshold=self.threshold)
                predictions_dict['tp'].extend(tp)
                predictions_dict['fp'].extend(fp)
                predictions_dict['tn'].extend(tn)
                predictions_dict['fn'].extend(fn)

        print(f'***** {type} Metrics ***** ')
        print(f'***** {type} Metrics ***** ', file=self.log_file)
        print(
                f"Loss: {'%.3f' % np.mean(self.test_batch_loss)} | Accuracy: {'%.3f' % np.mean(self.test_batch_accuracy)} | UAR: {'%.3f' % np.mean(self.test_batch_uar)}| F1:{'%.3f' % np.mean(self.test_batch_f1)} | Precision:{'%.3f' % np.mean(self.test_batch_precision)} | Recall:{'%.3f' % np.mean(self.test_batch_recall)}")
        print(
                f"Loss: {'%.3f' % np.mean(self.test_batch_loss)} | Accuracy: {'%.3f' % np.mean(self.test_batch_accuracy)} | UAR: {'%.3f' % np.mean(self.test_batch_uar)}| F1:{'%.3f' % np.mean(self.test_batch_f1)} | Precision:{'%.3f' % np.mean(self.test_batch_precision)} | Recall:{'%.3f' % np.mean(self.test_batch_recall)}",
                file=self.log_file)

        log_summary(self.writer, epoch, accuracy=np.mean(self.test_batch_accuracy),
                    loss=np.mean(self.test_batch_loss),
                    uar=np.mean(self.test_batch_uar), lr=self.optimiser.state_dict()['param_groups'][0]['lr'],
                    type=type)
        log_conf_matrix(self.writer, epoch, predictions_dict=predictions_dict, type=type)

        y = [element for sublist in y for element in sublist]
        predictions = [element for sublist in predictions for element in sublist]
        write_to_npy(filename=self.debug_filename, predictions=predictions, labels=y, epoch=epoch, accuracy=np.mean(
                self.test_batch_accuracy), loss=np.mean(self.test_batch_loss), uar=np.mean(self.test_batch_uar),
                     lr=self.optimiser.state_dict()['param_groups'][0]['lr'], predictions_dict=predictions_dict,
                     type=type)

    def train(self):

        # For purposes of calculating normalized values, call this method with train data followed by test
        train_data, train_labels = self.data_reader(
                self.data_read_path + 'train_challenge_with_d1_raw_16k_data_2sec.npy',
                self.data_read_path + 'train_challenge_with_d1_raw_16k_labels_2sec.npy',
                shuffle=True,
                train=True)
        dev_data, dev_labels = self.data_reader(self.data_read_path + 'dev_challenge_with_d1_raw_16k_data_2sec.npy',
                                                self.data_read_path + 'dev_challenge_with_d1_raw_16k_labels_2sec.npy',
                                                shuffle=False, train=False)
        test_data, test_labels = self.data_reader(self.data_read_path + 'test_challenge_with_d1_raw_16k_data_2sec.npy',
                                                  self.data_read_path + 'test_challenge_with_d1_raw_16k_labels_2sec.npy',
                                                  shuffle=False, train=False)

        # For the purposes of assigning pos weight on the fly we are initializing the cost function here
        self.loss_function = nn.BCEWithLogitsLoss(pos_weight=to_tensor(self.pos_weight, device=self.device))

        total_step = len(train_data)
        for epoch in range(1, self.epochs):
            self.network.train()
            self.batch_loss, self.batch_accuracy, self.batch_uar, self.batch_f1, self.batch_precision, self.batch_recall, audio_for_tensorboard_train = [], [], [], [], [], [], None
            for i, (audio_data, label) in enumerate(zip(train_data, train_labels)):
                self.optimiser.zero_grad()
                label = to_tensor(label, device=self.device).float()
                audio_data = to_tensor(audio_data, device=self.device)
                # if i == 0:
                #     self.writer.add_graph(self.network, audio_data)
                predictions = self.network(audio_data).squeeze(1)
                loss = self.loss_function(predictions, label)
                predictions = nn.Sigmoid()(predictions)
                loss.backward()
                self.optimiser.step()
                accuracy, uar, precision, recall, f1 = accuracy_fn(predictions, label, self.threshold)
                self.batch_loss.append(to_numpy(loss))
                self.batch_accuracy.append(to_numpy(accuracy))
                self.batch_uar.append(uar)
                self.batch_f1.append(f1)
                self.batch_precision.append(precision)
                self.batch_recall.append(recall)

                if i % self.display_interval == 0:
                    print("****************************************************************")
                    print("predictions mean", torch.mean(predictions).detach().cpu().numpy())
                    print("predictions mean", torch.mean(predictions).detach().cpu().numpy(), file=self.log_file)
                    print("predictions sum", torch.sum(predictions).detach().cpu().numpy())
                    print("predictions sum", torch.sum(predictions).detach().cpu().numpy(), file=self.log_file)
                    print("predictions range", torch.min(predictions).detach().cpu().numpy(),
                          torch.max(predictions).detach().cpu().numpy())
                    print("predictions range", torch.min(predictions).detach().cpu().numpy(),
                          torch.max(predictions).detach().cpu().numpy(), file=self.log_file)
                    print("predictions hist", np.histogram(predictions.detach().cpu().numpy()))
                    print("predictions hist", np.histogram(predictions.detach().cpu().numpy()), file=self.log_file)
                    print("predictions variance", torch.var(predictions).detach().cpu().numpy())
                    print("predictions variance", torch.var(predictions).detach().cpu().numpy(), file=self.log_file)
                    print("****************************************************************")
                    print(
                            f"Epoch: {epoch}/{self.epochs} | Step: {i}/{total_step} | Loss: {'%.3f' % loss} | Accuracy: {'%.3f' % accuracy} | UAR: {'%.3f' % uar}| F1:{'%.3f' % f1} | Precision: {'%.3f' % precision} | Recall: {'%.3f' % recall}")
                    print(
                            f"Epoch: {epoch}/{self.epochs} | Step: {i}/{total_step} | Loss: {'%.3f' % loss} | Accuracy: {accuracy} | UAR: {'%.3f' % uar}| F1:{'%.3f' % f1} | Precision: {'%.3f' % precision} | Recall: {'%.3f' % recall}",
                            file=self.log_file)

            # Decay learning rate
            # self.scheduler.step(epoch=epoch)
            log_summary(self.writer, epoch, accuracy=np.mean(self.batch_accuracy),
                        loss=np.mean(self.batch_loss),
                        uar=np.mean(self.batch_uar), lr=self.optimiser.state_dict()['param_groups'][0]['lr'],
                        type='Train')
            print('***** Overall Train Metrics ***** ')
            print('***** Overall Train Metrics ***** ', file=self.log_file)
            print(
                    f"Loss: {'%.3f' % np.mean(self.batch_loss)} | Accuracy: {'%.3f' % np.mean(self.batch_accuracy)} | UAR: {'%.3f' % np.mean(self.batch_uar)} | F1:{'%.3f' % np.mean(self.batch_f1)} | Precision:{'%.3f' % np.mean(self.batch_precision)} | Recall:{'%.3f' % np.mean(self.batch_recall)}")
            print(
                    f"Loss: {'%.3f' % np.mean(self.batch_loss)} | Accuracy: {'%.3f' % np.mean(self.batch_accuracy)} | UAR: {'%.3f' % np.mean(self.batch_uar)} | F1:{'%.3f' % np.mean(self.batch_f1)} | Precision:{'%.3f' % np.mean(self.batch_precision)} | Recall:{'%.3f' % np.mean(self.batch_recall)}",
                    file=self.log_file)
            print('Learning rate ', self.optimiser.state_dict()['param_groups'][0]['lr'])
            print('Learning rate ', self.optimiser.state_dict()['param_groups'][0]['lr'], file=self.log_file)
            # dev data
            self.run_for_epoch(epoch, dev_data, dev_labels, type='Dev')

            # test data
            self.run_for_epoch(epoch, test_data, test_labels, type='Test')

            if epoch % self.network_save_interval == 0:
                save_path = self.network_save_path + '/' + self.run_name + '_' + str(epoch) + '.pt'
                torch.save(self.network.state_dict(), save_path)
                print('Network successfully saved: ' + save_path)

    def test(self):
        test_data, test_labels = self.data_reader(self.data_read_path + 'test_challenge_data.npy',
                                                  self.data_read_path + 'test_challenge_labels.npy',
                                                  shuffle=False, train=False)
        test_predictions = self.network(test_data).squeeze(1)
        test_predictions = nn.Sigmoid()(test_predictions)
        test_accuracy, test_uar = accuracy_fn(test_predictions, test_labels, self.threshold)
        print(f"Accuracy: {test_accuracy} | UAR: {test_uar}")
        print(f"Accuracy: {test_accuracy} | UAR: {test_uar}", file=self.log_file)

    def infer(self, data_file):
        test_data = self.data_reader(data_file, shuffle=False, train=False, infer=True)
        test_predictions = self.network(test_data).squeeze(1)
        test_predictions = nn.Sigmoid()(test_predictions)
        return test_predictions