import os
from typing import Dict, Iterable, List, Mapping, Sequence, Union

import meerkat as mk
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torchmetrics
import wandb
from meerkat.nn import ClassificationOutputColumn
from omegaconf import DictConfig
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger
from terra.pytorch import TerraModule
from torch.utils.data import DataLoader, RandomSampler, WeightedRandomSampler
from torchmetrics.functional import dice_score
from torchvision import transforms as transforms
from torchvision.models.segmentation import fcn_resnet50

from domino.cnc import SupervisedContrastiveLoss, load_contrastive_dp
from domino.gdro_loss import LossComputer
from domino.modeling import DenseNet, ResNet, SequenceModel
from domino.utils import PredLogger

# from domino.data.iwildcam import get_iwildcam_model
# from domino.data.wilds import get_wilds_model

DOMINO_DIR = "/home/ksaab/Documents/domino"


def free_gpu(tensors, delete):
    for tensor in tensors:
        tensor = tensor.detach().cpu()
        if delete:
            del tensor


def get_save_dir(config):
    gaze_split = config["train"]["gaze_split"]
    target = config["dataset"]["target_column"]
    subgroup_columns = config["dataset"]["subgroup_columns"]
    subgroups = ""
    for name in subgroup_columns:
        subgroups += f"_{name}"
    subgroups = subgroups if len(subgroup_columns) > 0 else "none"
    method = config["train"]["method"]
    # if config["train"]["loss"]["gdro"]:
    #     method = "gdro"
    # elif config["train"]["loss"]["reweight_class"]:
    #     method = "reweight"
    # elif config["train"]["loss"]["robust_sampler"]:
    #     method = "sampler"
    # elif config["train"]["multiclass"]:
    #     method = "multiclass"
    # elif "upsampled" in config["dataset"]["datapanel_pth"]:
    #     method = "upsample"
    # elif config["train"]["method"] == "cnc":
    #     method = "cnc"
    # elif config["train"]["method"] == "cnc_gaze":
    #     method = "cnc_gaze"
    # elif config["train"]["method"] == "randcon":
    #     method = "randcon"

    lr = config["train"]["lr"]
    wd = config["train"]["wd"]
    dropout = config["model"]["dropout"]
    save_dir = f"{DOMINO_DIR}/scratch/khaled/results/method_{method}/gaze_split_{gaze_split}/target_{target}/subgroup_{subgroups}/lr_{lr}/wd_{wd}/dropout_{dropout}"

    if "erm" not in config["train"]["method"]:
        cw = config["train"]["contrastive_config"]["contrastive_weight"]
        save_dir += f"/cw_{cw}"

        # if method == "cnc_gaze":
        #     alpha = config["train"]["cnc_config"]["gaze_alpha"]
        #     save_dir += f"/alpha_{alpha}"

    seed = config["train"]["seed"]
    save_dir += f"/seed_{seed}"
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)
    return save_dir


def dictconfig_to_dict(d):
    """Convert object of type OmegaConf to dict so Wandb can log properly
    Support nested dictionary.
    """
    return {
        k: dictconfig_to_dict(v) if isinstance(v, DictConfig) else v
        for k, v in d.items()
    }


class Classifier(pl.LightningModule, TerraModule):
    def __init__(self, config: dict = None):
        super().__init__()
        self.config = config

        self.cnc = config["train"]["method"] == "cnc"
        self.cnc_gaze = config["train"]["method"] == "cnc_gaze"
        self.randcon = config["train"]["method"] == "randcon"
        self.gaze_clip = config["train"]["method"] == "gaze_clip"
        self.segmentation = config["train"]["method"] == "segmentation"
        self.contrastive = (
            "cnc" in config["train"]["method"] or "con" in config["train"]["method"]
        )

        self._set_model()
        criterion_dict = {"cross_entropy": nn.CrossEntropyLoss, "mse": nn.MSELoss}
        criterion_fnc = criterion_dict[config["train"]["loss"]["criterion"]]
        if config["train"]["loss"]["criterion"] == "cross_entropy":
            criterion = criterion_fnc(
                # weight=torch.Tensor(config["train"]["loss"]["class_weights"]).cuda(),
                reduction="none",
            )
        else:
            criterion = criterion_fnc(
                reduction="none",
            )

        loss_cfg = config["train"]["loss"]
        dataset_cfg = config["dataset"]

        if self.contrastive:
            self.contrastive_loss = SupervisedContrastiveLoss(
                config["train"]["contrastive_config"]
            )
            self.encoder = nn.Sequential(*list(self.model.children())[:-1])

            self.train_loss_computer = criterion
            self.val_loss_computer = criterion

        else:
            self.train_loss_computer = LossComputer(
                criterion,
                is_robust=loss_cfg["gdro"],
                dataset_config=dataset_cfg["train_dataset_config"],
                gdro_config=loss_cfg["gdro_config"],
            )
            self.val_loss_computer = LossComputer(
                criterion,
                is_robust=loss_cfg["gdro"],
                dataset_config=dataset_cfg["val_dataset_config"],
                gdro_config=loss_cfg["gdro_config"],
            )

        if config["train"]["loss"]["criterion"] == "mse" or self.segmentation:
            self.metrics = {}
        else:
            metrics = self.config.get("metrics", ["auroc", "accuracy"])
            self.set_metrics(metrics, num_classes=dataset_cfg["num_classes"])
        self.valid_preds = PredLogger()

    def set_metrics(self, metrics: List[str], num_classes: int = None):
        num_classes = (
            self.config["dataset"]["num_classes"]
            if num_classes is None
            else num_classes
        )
        _metrics = {
            "accuracy": torchmetrics.Accuracy(compute_on_step=False),
            "auroc": torchmetrics.AUROC(compute_on_step=False, num_classes=num_classes),
            # TODO (Sabri): Use sklearn metrics here, torchmetrics doesn't handle case
            # there are only a subset of classes in a test set
            "macro_f1": torchmetrics.F1(num_classes=num_classes, average="macro"),
            "macro_recall": torchmetrics.Recall(
                num_classes=num_classes, average="macro"
            ),
        }
        self.metrics = nn.ModuleDict(
            {name: metric for name, metric in _metrics.items() if name in metrics}
        )  # metrics need to be child module of the model, https://pytorch-lightning.readthedocs.io/en/stable/metrics.html#metrics-and-devices

    def _set_model(self):
        model_cfg = self.config["model"]
        num_classes = self.config["dataset"]["num_classes"]
        if self.config["train"]["method"] == "gaze_erm" or self.gaze_clip:
            gaze_enc_cfg = self.config["train"]["gaze_encoder_config"]
            model = SequenceModel(
                input_size=3,
                hidden_size=gaze_enc_cfg["hidden_size"],
                num_layers=gaze_enc_cfg["num_layers"],
                encoder=gaze_enc_cfg["encoder"],
                bidirectional=gaze_enc_cfg["bidirectional"],
                nheads=gaze_enc_cfg["nheads"],
                T=gaze_enc_cfg["T"],
            )
            if self.gaze_clip:
                self.gaze_encoder = model
                self.gaze_encoder.classifier = nn.Identity()
                self.clip_func = nn.CrossEntropyLoss(reduction="none")
            else:
                self.model = model
        if self.config["train"]["method"] != "gaze_erm":
            if self.segmentation:
                self.model = fcn_resnet50(
                    pretrained=False,
                    num_classes=num_classes,
                )

            elif model_cfg["model_name"] == "resnet":
                self.model = ResNet(
                    num_classes=num_classes,
                    arch=model_cfg["arch"],
                    dropout=model_cfg["dropout"],
                    pretrained=model_cfg["pretrained"],
                )
            elif model_cfg["model_name"] == "densenet":
                self.model = DenseNet(num_classes=num_classes, arch=model_cfg["arch"])
            else:
                raise ValueError(f"Model name {model_cfg['model_name']} not supported.")

        if self.gaze_clip:
            self.fc = self.model.fc
            self.model.fc = nn.Identity()
            self.image_proj = nn.Linear(
                self.fc[1].in_features, int(gaze_enc_cfg["hidden_size"] / 2)
            )

    def forward(self, x):
        if self.gaze_clip:
            return self.fc(self.model(x))
        return self.model(x)

    def training_step(self, batch, batch_idx):
        if self.contrastive:
            a_inputs, a_targets, a_group_ids = (
                batch["input"],
                batch["target"],
                batch["group_id"],
            )
            if self.cnc:
                p_entries, n_entries = batch["contrastive_input_pair"]
                all_p_inputs = p_entries[0]
                all_n_inputs = n_entries[0]
                all_p_targets = p_entries[1]
                all_n_targets = n_entries[1]

            contrastive_loss = 0
            pos_sim = 0
            neg_sim = 0
            for a_ix in range(len(a_inputs)):
                if self.cnc:
                    p_inputs = all_p_inputs[a_ix]
                    n_inputs = all_n_inputs[a_ix]
                    p_targets = all_p_targets[a_ix]
                    n_targets = all_n_targets[a_ix]

                elif self.cnc_gaze or self.randcon:
                    gaze_features = batch["gaze_features"]
                    a_gfeats = gaze_features[a_ix]
                    gfeat_dist = torch.norm(gaze_features - a_gfeats, dim=1)
                    if self.randcon:
                        gfeat_dist = torch.rand_like(gfeat_dist)
                    alpha = gfeat_dist.median()
                    same_class = a_targets == a_targets[a_ix]

                    # n_mask = gfeat_dist <= alpha
                    n_mask = torch.logical_and(gfeat_dist > alpha, same_class)
                    p_mask = torch.logical_and(gfeat_dist <= alpha, same_class)
                    # p_mask = gfeat_dist > alpha
                    # remove anchor from p_mask
                    p_mask[a_ix] = False

                    if p_mask.sum() < 2 or n_mask.sum() < 2:
                        continue

                    p_inputs = a_inputs[p_mask]
                    p_targets = a_targets[p_mask]
                    n_inputs = a_inputs[n_mask]
                    n_targets = a_targets[n_mask]

                c_loss, pos_sim_, neg_sim_ = self.contrastive_loss(
                    (a_inputs[a_ix], p_inputs, n_inputs), self.encoder
                )

                c_loss.backward()
                free_gpu([c_loss], delete=True)

                contrastive_loss += c_loss.item()
                pos_sim += pos_sim_.item()
                neg_sim += neg_sim_.item()

            contrastive_loss /= len(a_inputs)
            pos_sim /= len(a_inputs)
            neg_sim /= len(a_inputs)

            if self.cnc:
                inputs = torch.cat([a_inputs, p_inputs, n_inputs])
                targets = torch.cat([a_targets, p_targets, n_targets])
            else:
                inputs = a_inputs
                targets = a_targets

            # group_ids = (
            #     a_group_ids  # torch.cat([a_group_ids, p_group_ids, n_group_ids])
            # )

        elif self.gaze_clip:
            inputs, gaze_inputs, targets, group_ids = (
                batch["input"],
                batch["gaze_seq"],
                batch["target"],
                batch["group_id"],
            )
            embs = self.image_proj(self.model(inputs))

            seq_len = batch["gaze_seq_len"]
            gaze_embs = self.gaze_encoder(gaze_inputs, seq_len)

            labels = torch.ones_like(gaze_embs)[:, 0].long()
            logits = gaze_embs @ embs.T
            loss_t = self.clip_func(logits, labels)
            loss_i = self.clip_func(logits.T, labels)
            loss = (loss_i + loss_t) / 2
            contrastive_loss = loss.mean()

        elif self.segmentation:
            inputs, targets, group_ids = (
                batch["input"],
                batch["segmentation_target"].long(),
                batch["group_id"],
            )
            outs = self.model(inputs)["out"]
            loss = self.train_loss_computer.loss(outs, targets, group_ids)
            self.train_loss_computer.log_stats(self.log, is_training=True)
            self.log("train_loss", loss, on_step=True, logger=True)  # , sync_dist=True)
            dice = dice_score(outs, targets)
            self.log("train_dice", dice)

        else:
            input_key = (
                "gaze_seq" if self.config["train"]["method"] == "gaze_erm" else "input"
            )
            inputs, targets, group_ids = (
                batch[input_key],
                batch["target"],
                batch["group_id"],
            )

            if self.config["train"]["method"] == "gaze_erm":
                seq_len = batch["gaze_seq_len"]
                outs = self.model(inputs, seq_len)
            else:
                outs = self.forward(inputs)

            loss = self.train_loss_computer.loss(outs, targets, group_ids)
            self.train_loss_computer.log_stats(self.log, is_training=True)
            self.log("train_loss", loss, on_step=True, logger=True)  # , sync_dist=True)

        if self.contrastive or self.gaze_clip:
            outs = self.forward(inputs)
            if self.gaze_clip:
                loss = self.train_loss_computer.loss(outs, targets, group_ids)
                self.train_loss_computer.log_stats(self.log, is_training=True)
            else:
                loss = self.train_loss_computer(outs, targets.long()).mean()
            self.log("train_loss", loss, on_step=True, logger=True)  # , sync_dist=True)

            cw = self.config["train"]["contrastive_config"]["contrastive_weight"]
            loss = (1 - cw) * loss + cw * contrastive_loss
            self.log(
                "contrastive_loss",
                contrastive_loss,
                on_step=True,
                logger=True,
                # sync_dist=True,
            )

            if self.contrastive:
                self.log(
                    "positive_sim",
                    pos_sim,
                    on_step=True,
                    logger=True,
                )

                self.log(
                    "negative_sim",
                    neg_sim,
                    on_step=True,
                    logger=True,
                )

        return loss

    def validation_step(self, batch, batch_idx):

        if self.segmentation:
            inputs, targets, group_ids = (
                batch["input"],
                batch["segmentation_target"].long(),
                batch["group_id"],
            )
            outs = self.model(inputs)["out"]
            dice = dice_score(outs, targets)
            self.log("valid_dice", dice)

        else:
            input_key = (
                "gaze_seq" if self.config["train"]["method"] == "gaze_erm" else "input"
            )

            inputs, targets, group_ids, sample_id = (
                batch[input_key],
                batch["target"],
                batch["group_id"],
                batch["id"],
            )

            if self.config["train"]["method"] == "gaze_erm":
                seq_len = batch["gaze_seq_len"]
                outs = self.model(inputs, seq_len)
            else:
                outs = self.forward(inputs)

        if self.contrastive:
            loss = self.val_loss_computer(outs, targets).mean()
        else:
            loss = self.val_loss_computer.loss(outs, targets, group_ids)
        self.log("valid_loss", loss)  # , sync_dist=True)

        for metric in self.metrics.values():
            metric(torch.softmax(outs, dim=-1), targets)

        if not self.segmentation:
            self.valid_preds.update(torch.softmax(outs, dim=-1), targets, sample_id)

    def validation_epoch_end(self, outputs) -> None:
        if not (self.contrastive):
            self.val_loss_computer.log_stats(self.log)
        for metric_name, metric in self.metrics.items():
            self.log(f"valid_{metric_name}", metric.compute())  # , sync_dist=True)

    def test_epoch_end(self, outputs) -> None:
        return self.validation_epoch_end(outputs)

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def configure_optimizers(self):
        train_cfg = self.config["train"]
        optimizer = torch.optim.Adam(
            self.parameters(), lr=train_cfg["lr"], weight_decay=train_cfg["wd"]
        )
        return optimizer


class MTClassifier(Classifier):
    def training_step(self, batch, batch_idx):
        inputs, targets, _ = batch["input"], batch["target"], batch["id"]
        outs = self.forward(inputs)
        loss = nn.functional.cross_entropy(outs, targets)
        self.log("train_loss", loss, on_step=True, logger=True)  # , sync_dist=True)
        return loss

    def validation_step(self, batch, batch_idx):
        inputs, targets, sample_id = batch["input"], batch["target"], batch["id"]
        outs = self.forward(inputs)
        loss = nn.functional.cross_entropy(outs, targets)
        self.log("valid_loss", loss)  # , sync_dist=True)

        for metric in self.metrics.values():
            metric(torch.softmax(outs, dim=-1), targets)

        self.valid_preds.update(torch.softmax(outs, dim=-1), targets, sample_id)


def train(
    dp: mk.DataPanel,
    input_column: str,
    target_column: str,
    id_column: str,
    model: Classifier = None,
    config: dict = None,
    num_classes: int = 2,
    max_epochs: int = 50,
    samples_per_epoch: int = None,
    gpus: int = 1,  # Union[int, Iterable] = [0],
    num_workers: int = 10,
    batch_size: int = 16,
    train_split: str = "train",
    valid_split: str = "valid",
    weighted_sampling: bool = False,
    **kwargs,
):
    # Note from https://pytorch-lightning.readthedocs.io/en/0.8.3/multi_gpu.html: Make sure to set the random seed so that each model initializes with the same weights.
    pl.utilities.seed.seed_everything(config["train"]["seed"])

    multiclass = config["train"]["multiclass"]

    train_mask = dp["split"].data == train_split
    if config["train"]["gaze_split"]:
        # gaze train split is one where chest tube labels exist
        train_mask = np.logical_and(
            train_mask, dp["chest_tube"].data.astype(str) != "nan"
        )
        gaze_features = torch.stack(
            [
                torch.Tensor(dp["gaze_time"]),
                torch.Tensor(dp["gaze_unique"]),
                torch.Tensor(dp["gaze_diffusivity"]),
                torch.Tensor(dp["gaze_max_visit"]),
            ]
        ).T

    subgroup_columns = config["dataset"]["subgroup_columns"]
    if len(subgroup_columns) > 0:
        group_ids = dp[target_column].data
        for i in range(len(subgroup_columns)):
            group_ids = group_ids + (2 ** (i + 1)) * dp[subgroup_columns[i]]
    else:
        group_ids = dp[target_column].data
    group_ids[np.isnan(group_ids)] = -1
    if multiclass:
        # make sure gdro and robust sampler are off
        assert (
            not config["train"]["loss"]["robust_sampler"]
            and not config["train"]["loss"]["gdro"]
        )
        num_classes = num_classes * 2 * len(subgroup_columns)

    dp = mk.DataPanel.from_batch(
        {
            "input": dp[input_column],
            "target": dp[target_column]  # .astype(int)
            if not multiclass
            else group_ids.astype(int),  # group_ids become target labels
            "id": dp[id_column],
            "split": dp["split"],
            "group_id": group_ids.astype(int),
            "chest_tube": dp["chest_tube"],  # DEBUG
            "gaze_features": gaze_features,
            "gaze_seq": dp["padded_gaze_seq"],
            "gaze_seq_len": dp["gaze_seq_len"],
            "filepath": dp["filepath"],
            "segmentation_target": dp["segmentation_target"],
        }
    )

    val_mask = dp["split"].data == valid_split
    if config["train"]["method"] == "gaze_erm":
        train_split_frac = 0.8
        gaze_mask = np.array(
            [isinstance(entry, torch.FloatTensor) for entry in dp["gaze_seq"]]
        )
        rand_vec = np.random.rand(len(gaze_mask))
        train_mask = np.logical_and(gaze_mask, rand_vec < train_split_frac)
        val_mask = np.logical_and(gaze_mask, rand_vec >= train_split_frac)
    train_dp = dp.lz[train_mask]
    val_dp = dp.lz[val_mask]

    # create train_dataset_config and val_dataset_config
    subgroup_columns_ = []
    binary_strs = ["without", "with"]
    for i in range(2 ** (len(subgroup_columns) + 1)):
        subgroup_name = f"{binary_strs[(i%2)!=0]}_{target_column}"
        for ndx, name in enumerate(subgroup_columns):
            subgroup_name += f"_{binary_strs[(int(i/(2**(ndx+1)))%2)!=0]}_{name}"
        subgroup_columns_.append(subgroup_name)

    train_dataset_config = {
        "n_groups": len(subgroup_columns_),
        "group_counts": [
            int((train_dp["group_id"] == group_i).sum())
            for group_i in range(len(subgroup_columns_))
        ],
        "group_str": subgroup_columns_,
    }
    val_dataset_config = {
        "n_groups": len(subgroup_columns_),
        "group_counts": [
            int((val_dp["group_id"] == group_i).sum())
            for group_i in range(len(subgroup_columns_))
        ],
        "group_str": subgroup_columns_,
    }

    print(f"Train config: {train_dataset_config}")

    config["dataset"]["train_dataset_config"] = train_dataset_config
    config["dataset"]["val_dataset_config"] = val_dataset_config

    if config["train"]["loss"]["reweight_class"]:

        class_weights = np.array(
            [
                float(1 - ((train_dp["target"] == i).sum() / len(train_dp)))
                for i in range(num_classes)
            ]
        )
        class_weights = [
            int((1 + class_weight) ** config["train"]["loss"]["reweight_class_alpha"])
            for class_weight in class_weights
        ]

    else:
        class_weights = [1] * num_classes

    config["train"]["loss"]["class_weights"] = class_weights

    if (model is not None) and (config is not None):
        raise ValueError("Cannot pass both `model` and `config`.")

    if model is None:
        config = {} if config is None else config
        config["dataset"]["num_classes"] = num_classes
        if config["model"]["resume_ckpt"]:
            model = Classifier.load_from_checkpoint(
                checkpoint_path=config["model"]["resume_ckpt"], config=config
            )
        else:
            model = Classifier(config)

    save_dir = get_save_dir(config)
    logger = WandbLogger(
        config=dictconfig_to_dict(config),
        config_exclude_keys="wandb",
        save_dir=save_dir,
        **config["wandb"],
    )

    model.train()
    ckpt_metric = "valid_accuracy"
    mode = "max"
    if config["train"]["method"] == "segmentation":
        ckpt_metric = "valid_dice"
    # if len(subgroup_columns) > 0:
    #     ckpt_metric = "robust val acc"
    # if "erm" not in config["train"]["method"]:
    #     ckpt_metric = "contrastive_loss"
    #     mode = "min"
    checkpoint_callback = ModelCheckpoint(
        monitor=ckpt_metric, mode=mode, every_n_train_steps=5
    )
    trainer = pl.Trainer(
        gpus=gpus,
        max_epochs=max_epochs,
        accumulate_grad_batches=1,
        log_every_n_steps=1,
        logger=logger,
        callbacks=[checkpoint_callback],
        default_root_dir=save_dir,
        **kwargs,
    )
    # accelerator="dp",

    sampler = None

    if weighted_sampling:
        assert not config["train"]["loss"]["robust_sampler"]
        weights = torch.ones(len(train_dp))
        weights[train_dp["target"] == 1] = (1 - dp["target"]).sum() / (
            dp["target"].sum()
        )
        samples_per_epoch = (
            len(train_dp) if samples_per_epoch is None else samples_per_epoch
        )
        sampler = WeightedRandomSampler(weights=weights, num_samples=samples_per_epoch)

    elif config["train"]["loss"]["robust_sampler"]:
        weights = torch.ones(len(train_dp))
        for group_i in range(len(subgroup_columns_)):
            group_mask = train_dp["group_id"] == group_i
            # higher weight if rare subclass
            weights[group_mask] = (
                1
                + (len(train_dp) - (train_dp["group_id"] == group_i).sum())
                / len(train_dp)
            ) ** config["train"]["loss"]["reweight_class_alpha"]

        samples_per_epoch = (
            len(train_dp) if samples_per_epoch is None else samples_per_epoch
        )
        sampler = WeightedRandomSampler(weights=weights, num_samples=samples_per_epoch)
    elif samples_per_epoch is not None:
        sampler = RandomSampler(train_dp, num_samples=samples_per_epoch)

    # if doing CnC, we need to create the contrastive train dataloader
    if config["train"]["cnc"]:
        cnc_config = config["train"]["cnc_config"]
        contrastive_loader = load_contrastive_dp(
            train_dp,
            cnc_config["num_anchor"],
            cnc_config["num_positive"],
            cnc_config["num_negative"],
        )

        # train_dp_ = train_dp.copy()
        train_dp = contrastive_loader

    train_dl = DataLoader(
        train_dp,
        batch_size=batch_size,
        num_workers=num_workers,
        shuffle=sampler is None,
        sampler=sampler,
        drop_last=True,
    )
    valid_dl = DataLoader(
        val_dp,
        batch_size=batch_size,
        num_workers=num_workers,
    )

    trainer.fit(model, train_dl, valid_dl)
    wandb.finish()
    return model


def score(
    model: nn.Module,
    dp: mk.DataPanel,
    layers: Mapping[str, nn.Module] = None,
    reduction_fns: Sequence[callable] = None,
    input_column: str = "input",
    pbar: bool = True,
    device: int = 0,
    run_dir: str = None,
    **kwargs,
):
    model.to(device).eval()

    class ActivationExtractor:
        """Class for extracting activations a targetted intermediate layer"""

        def __init__(self, reduction_fn: callable = None):
            self.activation = None
            self.reduction_fn = reduction_fn

        def add_hook(self, module, input, output):
            if self.reduction_fn is not None:
                output = self.reduction_fn(output)
            self.activation = output

    layer_to_extractor = {}

    if layers is not None:
        for name, layer in layers.items():
            if reduction_fns is not None:
                for reduction_fn in reduction_fns:
                    extractor = ActivationExtractor(reduction_fn=reduction_fn)
                    layer.register_forward_hook(extractor.add_hook)
                    layer_to_extractor[name] = extractor
                    # layer_to_extractor[f"{name}_{reduction_fn.__name__}"] = extractor
            else:
                extractor = ActivationExtractor()
                layer.register_forward_hook(extractor.add_hook)
                layer_to_extractor[name] = extractor

    @torch.no_grad()
    def _score(batch: mk.DataPanel):
        x = batch[input_column].data.to(device)
        out = model(x)  # Run forward pass

        return {
            "output": ClassificationOutputColumn(logits=out.cpu(), multi_label=False),
            **{
                name: extractor.activation.cpu()
                for name, extractor in layer_to_extractor.items()
            },
        }

    dp = dp.update(
        function=_score,
        is_batched_fn=True,
        pbar=pbar,
        input_columns=[input_column],
        **kwargs,
    )
    return dp
