import argparse
import glob
import os
import socket
import timeit
from collections import OrderedDict
from datetime import datetime

import numpy as np
from matplotlib import pyplot as plt
from PIL import Image
from PIL.ImageMath import eval
from tqdm import tqdm

import imgaug as ia
import oyaml
# PyTorch includes
import torch
import torch.nn as nn
import torch.optim as optim
from attrdict import AttrDict
# Custom includes
from dataloaders import utils
from dataloaders.alphapilot import AlphaPilotSegmentation
from imgaug import augmenters as iaa
# from dataloaders import custom_transforms as tr
from networks import deeplab_resnet, deeplab_xception, unet
from tensorboardX import SummaryWriter
from termcolor import colored
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.utils import make_grid

# from inference import labels

# from train import labels

# parser = argparse.ArgumentParser()
# parser.add_argument("-b", "--batch_size", required=True, type=int, help="Num of images per batch for training")
# args = parser.parse_args()

###################### Load Config File #############################
CONFIG_FILE_PATH = 'config/config.yaml'
with open(CONFIG_FILE_PATH) as fd:
    config_yaml = oyaml.load(fd)  # Returns an ordered dict. Used for printing

config = AttrDict(config_yaml)
print(colored('Config being used for training:\n{}\n\n'.format(oyaml.dump(config_yaml)), 'green'))


# Setting parameters
nEpochs = 100  # Number of epochs for training
resume_epoch = 0  # Default is 0, change if want to resume


p = OrderedDict()  # Parameters to include in report
p['trainBatchSize'] = config.train.batchSize  # Training batch size
testBatchSize = 1 # Testing batch size
useTest = True  # See evolution of the test set when training
nTestInterval = 1  # Run on test set every nTestInterval epochs
snapshot = 2  # Store a model every snapshot epochs

p['nAveGrad'] = 1  # Average the gradient of several iterations
p['lr'] = 1e-6  # Learning rate
p['wd'] = 5e-2  # Weight decay
p['momentum'] = 0.9  # Momentum
p['epoch_size'] = 1  # How many epochs to change learning rate

p['Model'] = 'deeplab'  # Choose model: unet or deeplab
backbone = 'xception'  # For deeplab only: Use xception or resnet as feature extractor,
num_of_classes = 2
imsize = 512  # 256 or 512
output_stride = 8 # 8 or 16, 8 is better. Controls output stride of the deeplab model, which increases resolution of convolutions.
numInputChannels = 3

def save_test_img(inputs, outputs, ii):

    fig = plt.figure()
    ax0 = plt.subplot(121)
    ax1 = plt.subplot(122)

    # Input RGB img
    rgb_img = inputs[0]
    # inv_normalize = transforms.Normalize(
    #     mean=[-0.5 / 0.5, -0.5 / 0.5, -0.5 / 0.5],
    #     std=[1 / 0.5, 1 / 0.5, 1 / 0.5]
    # )
    # rgb_img = inv_normalize(rgb_img)
    rgb_img = rgb_img.detach().cpu().numpy()
    rgb_img = np.transpose(rgb_img, (1, 2, 0))

    # Inference Result
    predictions = torch.max(outputs[:1], 1)[1].detach().cpu().numpy()
    output_rgb = utils.decode_seg_map_sequence(predictions)
    output_rgb = output_rgb.numpy()
    output_rgb = np.transpose(output_rgb[0], (1, 2, 0))

    # Create plot
    ax0.imshow(rgb_img)
    ax0.set_title('Source RGB Image')  # subplot 211 title
    ax1.imshow(output_rgb)
    ax1.set_title('Inference result')

    fig.savefig('data/results/current_training_model/%04d-results.png' % (ii))
    plt.close('all')


save_dir_root = os.path.join(os.path.dirname(os.path.abspath(__file__)))
exp_name = os.path.dirname(os.path.abspath(__file__)).split('/')[-1]

if resume_epoch != 0:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) if runs else 0
else:
    runs = sorted(glob.glob(os.path.join(save_dir_root, 'run', 'run_*')))
    run_id = int(runs[-1].split('_')[-1]) + 1 if runs else 0
print('run id: ', run_id)

save_dir = os.path.join(save_dir_root, 'run', 'run_{:02d}'.format(run_id))


# Network definition
if p['Model'] == 'deeplab':
    if backbone == 'xception':
        net = deeplab_xception.DeepLabv3_plus(nInputChannels=numInputChannels, n_classes=num_of_classes, os=output_stride, pretrained=True)
    elif backbone == 'resnet':
        net = deeplab_resnet.DeepLabv3_plus(nInputChannels=numInputChannels, n_classes=num_of_classes, os=output_stride, pretrained=True)
    else:
        raise NotImplementedError
    modelName = 'deeplabv3plus-' + backbone

    # Use the following optimizer
    optimizer = optim.SGD(net.parameters(), lr=p['lr'], momentum=p['momentum'], weight_decay=p['wd'])
    p['optimizer'] = str(optimizer)

    # Use the following loss function
    criterion = utils.cross_entropy2d
elif p['Model'] == 'unet':
    net = unet.Unet(num_classes=num_of_classes)
    modelName = 'unet'

    # Use the following optimizer
    optimizer = torch.optim.Adam(net.parameters(), lr=0.0001, weight_decay=0.0001)
    exp_lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=7, gamma=0.1)
    p['optimizer'] = str(optimizer)

    # Use the following loss function
    criterion = nn.CrossEntropyLoss(size_average=False, reduce=True)
else:
    raise NotImplementedError



device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
#criterion = criterion.to(device) #TODO: IS THIS NEEDED?



# Enable Multi-GPU training
if torch.cuda.device_count() > 1:
    print("Let's use", torch.cuda.device_count(), "GPUs!")
    # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
    net = nn.DataParallel(net)


if resume_epoch == 0:
    print("Training deeplabv3+ from scratch...")
else:
    print("Initializing weights from: {}...".format(
        os.path.join(save_dir, 'models', modelName + '_epoch-' + str(resume_epoch - 1) + '.pth')))
    net.load_state_dict(
        torch.load(os.path.join(save_dir, 'models', modelName + '_epoch-' + str(resume_epoch - 1) + '.pth'),
                   map_location=lambda storage, loc: storage))  # Load all tensors onto the CPU

net.to(device)


if resume_epoch != nEpochs:
    # Logging into Tensorboard
    log_dir = os.path.join(save_dir, 'models', datetime.now().strftime(
        '%b%d_%H-%M-%S') + '_' + socket.gethostname())
    writer = SummaryWriter(log_dir=log_dir)


    augs_train = iaa.Sequential([
        # Geometric Augs
        iaa.Resize((imsize, imsize), 0)
        # iaa.Fliplr(0.5),
        # iaa.Flipud(0.5),
        # iaa.Rot90((0, 4)),

        # # Blur and Noise
        # iaa.Sometimes(0.10, iaa.OneOf([iaa.GaussianBlur(sigma=(1.5, 2.5), name="gaus_blur"),
        #                             iaa.MotionBlur(k=13, angle=[0, 180, 270, 360], direction=[-1.0, 1.0],
        #                                             name='motion_blur'),
        #                             ])),

        # # Color, Contrast, etc
        # iaa.Sometimes(0.30, iaa.CoarseDropout(0.05, size_px=(2, 4), per_channel=0.5, min_size=2, name='dropout')),

        # iaa.SomeOf((0, None), [ iaa.Sometimes(0.15, iaa.GammaContrast((0.5, 1.5), name="contrast")),
        #                         iaa.Sometimes(0.15, iaa.Multiply((0.40, 1.60), per_channel=1.0, name="multiply")),
        #                         iaa.Sometimes(0.15, iaa.AddToHueAndSaturation((-30, 30), name="hue_sat")),
        #                     ]),

        # # Affine
        # iaa.Sometimes(0.10, iaa.Affine(scale={"x": (0.5, 0.7), "y": 1.0})),
        # iaa.Sometimes(0.10, iaa.Affine(scale=(0.5, 0.7))),
    ])

    augs_test = iaa.Sequential([
        # Geometric Augs
        iaa.Resize((imsize, imsize), interpolation='cubic'),
    ])

    # db_train = AlphaPilotSegmentation(
    #     input_dir=config.train.datasets.images, label_dir=config.train.datasets.labels,
    #     transform=augs_train,
    #     input_only=["gaus_blur", "motion_blur", "dropout", "contrast", "multiply",  "hue_sat"]
    # )
    db_train_list = []
    for dataset in config.train.datasets:
        db = AlphaPilotSegmentation(input_dir=dataset.images, label_dir=dataset.labels,
                                              transform=augs_train, input_only=None)
        train_size = int(config.train.percentageDataForTraining * len(db))
        db = torch.utils.data.Subset(db, range(train_size))
        db_train_list.append(db)

    db_train = torch.utils.data.ConcatDataset(db_train_list)

    for dataset in config.eval.datasetsSynthetic:
        db_validation = AlphaPilotSegmentation(
            input_dir=dataset.images, label_dir=dataset.labels,
            transform=augs_train,
            input_only=None
        )
    db_test_list = []
    for dataset in config.eval.datasetsReal:
        db_test = AlphaPilotSegmentation(input_dir=dataset.images, transform=augs_test, input_only=None)
        db_test_list.append(db_test)

    print('size db_train, db_val: ', len(db_train), len(db_validation))

    trainloader = DataLoader(db_train, batch_size=p['trainBatchSize'], shuffle=True, num_workers=4, drop_last=True)
    validationloader = DataLoader(db_validation, batch_size=p['trainBatchSize'], shuffle=False, num_workers=4, drop_last=True)
    testloader = DataLoader(db_test, batch_size=p['trainBatchSize'], shuffle=False, num_workers=4, drop_last=True)

    utils.generate_param_report(os.path.join(save_dir, exp_name + '.txt'), p)

    num_img_tr = len(trainloader)
    num_img_val = len(validationloader)
    num_img_ts = len(testloader)
    running_loss_tr = 0.0
    running_loss_val = 0.0
    running_loss_ts = 0.0
    aveGrad = 0
    global_step = 0
    print("Training Network")

# Main Training and Testing Loop
for epoch in range(resume_epoch, nEpochs):
    start_time = timeit.default_timer()

    #TODO: plot the learning rate
    if p['Model'] == 'unet':
        exp_lr_scheduler.step()
    else:
        if epoch % p['epoch_size'] == p['epoch_size'] - 1:
            lr_ = utils.lr_poly(p['lr'], epoch, nEpochs, 0.9)
            print('(poly lr policy) learning rate: ', lr_)
            optimizer = optim.SGD(net.parameters(), lr=lr_, momentum=p['momentum'], weight_decay=p['wd'])

    net.train()

    for ii, sample_batched  in enumerate(tqdm(trainloader)):
        inputs, labels, sample_filename = sample_batched

        inputs = inputs.to(device)
        labels = labels.to(device)
        global_step += 1

        # print('iter_num: ', ii + 1, '/', num_img_tr)
        writer.add_scalar('Epoch Num', epoch, global_step)

        torch.set_grad_enabled(True)
        outputs = net.forward(inputs)

        labels = labels.squeeze(1)
        loss = criterion(outputs, labels, size_average=False, batch_average=True)

        running_loss_tr += loss.item()

        # Print stuff
        if ii % num_img_tr == (num_img_tr - 1):
            running_loss_tr = running_loss_tr / num_img_tr
            writer.add_scalar('data/total_loss_epoch', running_loss_tr, global_step)
            print('[Epoch: %d, numImages: %5d]' % (epoch, ii * p['trainBatchSize'] + inputs.shape[0]))
            print('Loss: %f' % running_loss_tr)
            running_loss_tr = 0
            stop_time = timeit.default_timer()
            print("Execution time: " + str(stop_time - start_time) + "\n")

        # Backward the averaged gradient
        loss /= p['nAveGrad']
        loss.backward()
        aveGrad += 1

        # Update the weights once in p['nAveGrad'] forward passes
        if aveGrad % p['nAveGrad'] == 0:
            writer.add_scalar('data/total_loss_iter', loss.item(), global_step)
            optimizer.step()
            optimizer.zero_grad()
            aveGrad = 0

        # Show 10 * 3 images results each epoch
        if num_img_tr < 10:
            plot_per_iter =  num_img_tr
        else:
            plot_per_iter = 10
        if ii % (num_img_tr // plot_per_iter) == 0:
            img_tensor = torch.squeeze((inputs[:3].clone().cpu().data), 0)

            output_tensor = torch.squeeze(utils.decode_seg_map_sequence(torch.max(outputs[:3], 1)[1].detach().cpu().numpy()).type(torch.FloatTensor), 0)

            label_tensor = torch.squeeze(utils.decode_seg_map_sequence(torch.squeeze(labels[:3], 1).detach().cpu().numpy()).type(torch.FloatTensor), 0)
            images = []
            for img, output, label in zip(img_tensor, output_tensor, label_tensor):
                images.append(img)
                images.append(output)
                images.append(label)

            grid_image = make_grid(images ,3, normalize=True, scale_each=True )
            writer.add_image('Train', grid_image, global_step)


    # Save the model
    # TODO : bring the model to cpu before saving
    if (epoch % snapshot) == snapshot - 1:
        torch.save(net.state_dict(), os.path.join(save_dir, 'models', modelName + '_epoch-' + str(epoch) + '.pth'))
        print("Save model at {}\n".format(os.path.join(save_dir, 'models', modelName + '_epoch-' + str(epoch) + '.pth')))

    # One testing epoch
    if useTest and epoch % nTestInterval == (nTestInterval - 1):

        net.eval()
        images_list = []
        dataloader_list = [validationloader, testloader]
        for dataloader in dataloader_list:
            total_iou = 0.0

            if dataloader == validationloader:
                print("Validation Running")
            if dataloader == testloader:
                print("Testing Running")

            for ii, sample_batched in enumerate(tqdm(dataloader)):
                inputs, labels, sample_filename = sample_batched

                # Forward pass of the mini-batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                with torch.no_grad():
                    outputs = net.forward(inputs)

                predictions = torch.max(outputs, 1)[1]

                labels = labels.squeeze(1)
                loss = criterion(outputs, labels)


                # run validation dataset
                if dataloader == validationloader:
                    # print('iter_num: ', ii + 1, '/', num_img_val)
                    running_loss_val += loss.item()

                    _total_iou, per_class_iou, num_images_per_class = utils.get_iou(predictions, labels, n_classes=num_of_classes)
                    total_iou += _total_iou
                    # Print stuff
                    if ii % num_img_val == num_img_val - 1:
                        miou = total_iou / (ii * p['trainBatchSize'] + inputs.shape[0])
                        running_loss_val = running_loss_val / num_img_val

                        print('Validation:')
                        print('[Epoch: %d, numImages: %5d]' % (epoch, ii * p['trainBatchSize'] + inputs.shape[0]))
                        writer.add_scalar('data/val_loss_epoch', running_loss_val, global_step)
                        writer.add_scalar('data/val_miour', miou, global_step)
                        print('Loss: %f' % running_loss_val)
                        print('MIoU: %f\n' % miou)
                        running_loss_val = 0
                        print(inputs.shape)
                        # Show 10 * 2 images results each epoch
                        img_tensor = (inputs[:2].clone().cpu().data)
                        output_tensor = utils.decode_seg_map_sequence(torch.max(outputs[:2], 1)[1].detach().cpu().numpy()).type(torch.FloatTensor)
                        label_tensor = utils.decode_seg_map_sequence(torch.squeeze(labels[:2], 1).detach().cpu().numpy()).type(torch.FloatTensor)

                        images_list = []
                        for i in range (0,2):
                            images_list.append(img_tensor[i])
                            images_list.append(output_tensor[i])
                            images_list.append(label_tensor[i])

                        grid_image = make_grid(images_list ,3, normalize=True, scale_each=True )
                        writer.add_image('Validation', grid_image, global_step)


                if dataloader == testloader:
                    # print('iter_num: ', ii + 1, '/', num_img_ts)
                    running_loss_ts += loss.item()

                    _total_iou, per_class_iou, num_images_per_class = utils.get_iou(predictions, labels, n_classes=num_of_classes)
                    total_iou += _total_iou
                    # print stuff
                    save_test_img(inputs, outputs, ii)
                    if ii % num_img_ts == num_img_ts - 1:
                        # Calculate the loss and plot the graph
                        miou = total_iou / (ii * p['trainBatchSize'] + inputs.shape[0])
                        running_loss_ts = running_loss_ts / num_img_ts

                        print('Test:')
                        print('[Epoch: %d, numImages: %5d]' % (epoch, ii * testBatchSize + inputs.shape[0]))
                        writer.add_scalar('data/test_loss_epoch', running_loss_ts, global_step)
                        writer.add_scalar('data/test_miour', miou, global_step)
                        print('Loss: %f' % running_loss_ts)
                        print('MIoU: %f\n' % miou)
                        running_loss_ts = 0

                        # Show 10 * 3 images results each epoch
                        img_tensor = inputs[:1].clone().cpu().data
                        output_tensor = utils.decode_seg_map_sequence(torch.max(outputs[:1], 1)[1].detach().cpu().numpy()).type(torch.FloatTensor)
                        label_tensor = utils.decode_seg_map_sequence(torch.squeeze(labels[:1], 1).detach().cpu().numpy()).type(torch.FloatTensor)

                        images_list.append(img_tensor[0])
                        images_list.append(output_tensor[0])
                        images_list.append(label_tensor[0])

                        grid_image = make_grid(images_list, 3, normalize=True, scale_each=True ) #TODO: Probably shouldn't scale each. And should give min-max range for normalization.
                        writer.add_image('Test', grid_image, global_step)


writer.close()
