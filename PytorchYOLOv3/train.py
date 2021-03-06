from __future__ import division

from PytorchYOLOv3.models import *
from utils.logger import *
from utils.utils import *
from utils.datasets import *
from utils.parse_config import *
from PytorchYOLOv3.test import evaluate
from PytorchYOLOv3.valid import valid

from terminaltables import AsciiTable

import os
import sys
import time
import datetime
import argparse

import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision import transforms
from torch.autograd import Variable
import torch.optim as optim
torch.cuda.current_device()#han添加

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=2, help="number of epochs")
    parser.add_argument("--batch_size", type=int, default=2, help="size of each image batch")#将8改为了2
    parser.add_argument("--gradient_accumulations", type=int, default=2, help="number of gradient accums before step")
    parser.add_argument("--model_def", type=str, default="../config/VortoxYOLOv3.cfg", help="path to model definition file")
    parser.add_argument("--data_config", type=str, default="../config/Vortox.data", help="path to data config file")
    parser.add_argument("--pretrained_weights", type=str, default= "../PytorchYOLOv3/checkpoints/yolov3_vortox_19.pth", help="if specified starts from checkpoint model")#default= "../PytorchYOLOv3/checkpoints/yolov3_vortox_18.pth",
    parser.add_argument("--n_cpu", type=int, default=1, help="number of cpu threads to use during batch generation")#将8改成了1
    parser.add_argument("--img_size", type=int, default=416, help="size of each image dimension")
    parser.add_argument("--checkpoint_interval", type=int, default=1, help="interval between saving model weights")
    parser.add_argument("--evaluation_interval", type=int, default=1, help="interval evaluations on validation set")
    parser.add_argument("--compute_map", default=False, help="if True computes mAP every tenth batch")
    parser.add_argument("--multiscale_training", default=True, help="allow for multi-scale training")
    opt = parser.parse_args()
    print("opt = ", opt)

    logger = Logger("logs")

    #device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    device = torch.device("cpu")
    print("device = "+device.type)
    print("\ndevice_name: {}".format(torch.cuda.get_device_name(0)))

    print("Hello World, Hello PyTorch {}".format(torch.__version__))

    print("\nCUDA is available:{}, version is {}".format(torch.cuda.is_available(), torch.version.cuda))
    os.makedirs("output", exist_ok=True)
    os.makedirs("checkpoints", exist_ok=True)

    # Get data configuration
    data_config = parse_data_config(opt.data_config)
    train_path = data_config["train"]
    valid_path = data_config["valid"]
    class_names = load_classes(data_config["names"])

    # Initiate model
    model = Darknet(opt.model_def).to(device)
    model.apply(weights_init_normal)

    # If specified we start from checkpoint
    mAP_max = 0.
    AP0_max = 0.
    #begin_epoch = 0#从初始权重训练
    if opt.pretrained_weights:
        print("从checkpoints点开始训练:....")
        if opt.pretrained_weights.endswith(".pth"):
            model.load_state_dict(torch.load(opt.pretrained_weights))
            begin_epoch = int(opt.pretrained_weights.split('_')[-1][0:-4])+1 #加载预训练状态后，记录模型状态的epoch起始值：训练起始点+1
            print("\n---- Evaluating Model ----")
            # Evaluate the model on the validation set
            precision, recall, AP, f1, ap_class = valid(
                model,
                path=valid_path,
                iou_thres=0.5,
                conf_thres=0.5,
                nms_thres=0.5,
                img_size=opt.img_size,
                batch_size=1,
            )
            evaluation_metrics = [
                ("val_precision", precision.mean()),
                ("val_recall", recall.mean()),
                ("val_mAP", AP.mean()),
                ("val_f1", f1.mean()),
            ]
            logger.list_of_scalars_summary(evaluation_metrics, 0)

            # Print class APs and mAP
            ap_table = [["Index", "Class name", "AP"]]
            for i, c in enumerate(ap_class):
                ap_table += [[c, class_names[c], "%.5f" % AP[i]]]
            print(AsciiTable(ap_table).table)
            print(f"---- mAP {AP.mean()}",",AP[0] = {}".format(AP[0]))
            mAP_max = AP.mean()
            AP0_max = AP[0]
        else:
            model.load_darknet_weights(opt.pretrained_weights)

    # Get dataloader
    print("trainpath={}".format(train_path))
    train_transform = transforms.Compose(
        [
            transforms.ColorJitter(brightness=0.5,hue=0.3),
            transforms.RandomHorizontalFlip(p=0.5),
            transforms.RandomRotation(degrees = 30),
            transforms.RandomGrayscale(p=0.2),
            transforms.ToTensor(),
        ]
    )
    dataset = ListDataset(train_path, augment=False, multiscale=opt.multiscale_training)
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=opt.batch_size,
        shuffle=True,
        num_workers=opt.n_cpu,
        pin_memory=True,
        collate_fn=dataset.collate_fn,
    )

    optimizer = torch.optim.Adam(model.parameters())

    metrics = [
        "grid_size",
        "loss",
        "x",
        "y",
        "w",
        "h",
        "conf",
        "cls",
        "cls_acc",
        "recall50",
        "recall75",
        "precision",
        "conf_obj",
        "conf_noobj",
    ]


    for epoch in range(opt.epochs):
        model.train()
        start_time = time.time()
        for batch_i, (_, imgs, targets) in enumerate(dataloader):
            batches_done = len(dataloader) * epoch + batch_i

            imgs = Variable(imgs.to(device))
            targets = Variable(targets.to(device), requires_grad=False)

            loss, outputs = model(imgs, targets)
            loss.backward()

            if batches_done % opt.gradient_accumulations:
                # Accumulates gradient before each step
                optimizer.step()
                optimizer.zero_grad()

            # ----------------
            #   Log progress
            # ----------------

            log_str = "\n---- [Epoch %d/%d, Batch %d/%d] ----\n" % (epoch, opt.epochs, batch_i, len(dataloader))

            metric_table = [["Metrics", *[f"YOLO Layer {i}" for i in range(len(model.yolo_layers))]]]

            # Log metrics at each YOLO layer
            for i, metric in enumerate(metrics):
                formats = {m: "%.6f" for m in metrics}
                formats["grid_size"] = "%2d"
                formats["cls_acc"] = "%.2f%%"
                row_metrics = [formats[metric] % yolo.metrics.get(metric, 0) for yolo in model.yolo_layers]
                metric_table += [[metric, *row_metrics]]

                # Tensorboard logging
                tensorboard_log = []
                for j, yolo in enumerate(model.yolo_layers):
                    for name, metric in yolo.metrics.items():
                        if name != "grid_size":
                            tensorboard_log += [(f"{name}_{j+1}", metric)]
                tensorboard_log += [("loss", loss.item())]
                logger.list_of_scalars_summary(tensorboard_log, batches_done)

            log_str += AsciiTable(metric_table).table
            log_str += f"\nTotal loss {loss.item()}"

            # Determine approximate time left for epoch
            epoch_batches_left = len(dataloader) - (batch_i + 1)
            time_left = datetime.timedelta(seconds=epoch_batches_left * (time.time() - start_time) / (batch_i + 1))
            log_str += f"\n---- ETA {time_left}"

            print(log_str)

            model.seen += imgs.size(0)

        if epoch % opt.evaluation_interval == 0:
            print("\n---- Evaluating Model ----")
            # Evaluate the model on the validation set
            precision, recall, AP, f1, ap_class = valid(
                model,
                path=valid_path,
                iou_thres=0.5,
                conf_thres=0.5,
                nms_thres=0.5,
                img_size=opt.img_size,
                batch_size=1,
            )
            if precision == None:
                continue
            evaluation_metrics = [
                ("val_precision", precision.mean()),
                ("val_recall", recall.mean()),
                ("val_mAP", AP.mean()),
                ("val_f1", f1.mean()),
            ]
            logger.list_of_scalars_summary(evaluation_metrics, epoch)

            # Print class APs and mAP
            ap_table = [["Index", "Class name", "AP"]]
            for i, c in enumerate(ap_class):
                ap_table += [[c, class_names[c], "%.5f" % AP[i]]]
            print(AsciiTable(ap_table).table)
            print(f"---- mAP {AP.mean()}")
            if AP[0]>AP0_max :
                AP0_max = AP[0]
                print("save!")
                torch.save(model.state_dict(), f"checkpoints/yolov3_vortox_%d.pth" % (begin_epoch+epoch))

        # if epoch % opt.checkpoint_interval == 0:
        #     print("save!")
        #     torch.save(model.state_dict(), f"checkpoints/yolov3_vortox_%d.pth" % (epoch+30))
