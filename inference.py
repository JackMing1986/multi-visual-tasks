import json
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np

if 'MVT_ROOT' in os.environ:
    MVT_ROOT = os.getenv('MVT_ROOT')
    print('Get MVT_ROOT: ', MVT_ROOT)
else:
    MVT_ROOT = str(Path(__file__).absolute().parent)
    os.environ['MVT_ROOT'] = MVT_ROOT
    print('Set MVT_ROOT: ', MVT_ROOT)
    sys.path.insert(0, MVT_ROOT)
    print('Add {} to PYTHONPATH'.format(MVT_ROOT))

WORKERS_PER_DEVICE = 0

import torch

torch.multiprocessing.set_sharing_strategy('file_system')

from model.configs import cfg
from model.mvt.cores.metric_ops import LpDistance
from model.mvt.datasets.data_builder import build_dataloader, build_dataset
from model.mvt.models.model_builder import build_model
from model.mvt.utils.checkpoint_util import load_checkpoint
from model.mvt.utils.config_util import get_dataset_global_args, get_task_cfg
from model.mvt.utils.geometric_util import imresize
from model.mvt.utils.misc_util import ProgressBar
from model.mvt.utils.parallel_util import DataParallel
from model.mvt.utils.photometric_util import tensor2imgs


def det_single_device_test(model, data_loader, score_thr=0.05):
    model.eval()
    results = []
    img_names = []
    img_ids = []
    dataset = data_loader.dataset
    prog_bar = ProgressBar(len(dataset))

    for _, data in enumerate(data_loader):
        for i in range(len(data['img_metas'])):
            data['img_metas'][i] = data['img_metas'][i].data[0]
        with torch.no_grad():
            result = model(return_loss=False, rescale=True, **data)

        batch_size = len(result)

        if batch_size == 1 and isinstance(data['img'][0], torch.Tensor):
            img_tensor = data['img'][0]
        else:
            img_tensor = data['img'][0].data
        img_metas = data['img_metas'][0]
        imgs = tensor2imgs(img_tensor, **img_metas[0]['img_norm_cfg'])
        assert len(imgs) == len(img_metas)

        for i, (img, img_meta) in enumerate(zip(imgs, img_metas)):
            h, w, _ = img_meta['img_shape']
            img_show = img[:h, :w, :]

            ori_h, ori_w = img_meta['ori_shape'][:-1]
            img_show = imresize(img_show, (ori_w, ori_h))

            single_res = model.module.show_result(
                img_show, result[i],
                show=False, bbox_color='red',
                text_color='red', out_file=None, score_thr=score_thr)

            img_names.append(img_meta['ori_filename'])
            img_ids.append(img_meta['img_id'])
            results.append(single_res)

        for _ in range(batch_size):
            prog_bar.update()

    outputs = {
        'img_names': img_names,
        'img_ids': img_ids,
        'detections': results}
    return outputs


def save_det_json(data_dict, json_path):
    img_names = data_dict['img_names']
    img_ids = data_dict['img_ids']
    det_results = data_dict['detections']

    images = []
    annotations = []
    bbox_id = 0
    for i, det_result in enumerate(det_results):
        img_id = img_ids[i]
        img_info = {
            "file_name": img_names[i],
            "id": img_id}
        images.append(img_info)
        for j in range(len(det_result)):
            anno_info = {
                "image_id": img_id,
                "id": bbox_id,
                "bbox": [
                    int(det_result[j, 0] + 0.5),
                    int(det_result[j, 1] + 0.5),
                    int(det_result[j, 2] - det_result[j, 0] + 0.5),
                    int(det_result[j, 3] - det_result[j, 1] + 0.5)],
                "category_id": det_result[j, 5],
                "score": det_result[j, 4]
            }
            annotations.append(anno_info)
            bbox_id += 1
    predictions = {"images": images, "annotations": annotations}

    with open(json_path, "w") as wf:
        json.dump(predictions, wf)

    print('Detections have been saved at {}'.format(json_path))


def emb_single_device_test(model, data_loader, with_label=False):
    model.eval()
    results = []
    labels = []
    bbox_ids = []

    dataset = data_loader.dataset
    prog_bar = ProgressBar(len(dataset))

    for _, data in enumerate(data_loader):
        data['img_metas'] = data['img_metas'].data[0]
        data['img'] = data['img'].data[0]
        if with_label:
            data['label'] = data['label'].data[0]
        else:
            bbox_id_batch = data['bbox_id'].data.cpu().numpy()

        if 'bbox' in data:
            data['bbox'] = data['bbox'].data[0]

        with torch.no_grad():
            result = model(return_loss=False, rescale=True, **data)

        batch_size = len(result)
        for i in range(batch_size):
            results.append(result[i])
            if with_label:
                labels.append(data['label'][i].data.cpu().numpy())
            else:
                bbox_ids.append(bbox_id_batch[i])

        for _ in range(batch_size):
            prog_bar.update()

    results = np.array(results)

    outputs = {'embeddings': results}

    if with_label:
        labels = np.array(labels)
        outputs['labels'] = labels
        cache_path = '/tmp/emb_ref.pkl'
    else:
        bbox_ids = np.array(bbox_ids)
        outputs['bbox_ids'] = bbox_ids
        cache_path = '/tmp/emb_qry.pkl'

    # with open(cache_path, 'wb') as f:
    #    pickle.dump(outputs, f)

    return outputs


def run_det_task(cfg_path, model_path, json_path, score_thr):
    print('Running detection task ...')
    det_cfg = cfg.clone()
    get_task_cfg(det_cfg, cfg_path)

    # build the dataloader
    dataset_args = get_dataset_global_args(det_cfg.DATA)
    dataset = build_dataset(
        det_cfg.DATA.TEST_DATA, det_cfg.DATA.TEST_TRANSFORMS, dataset_args)
    data_loader = build_dataloader(
        dataset,
        samples_per_device=8,  # det_cfg.DATA.TEST_DATA.SAMPLES_PER_DEVICE,
        workers_per_device=WORKERS_PER_DEVICE,  # det_cfg.DATA.TEST_DATA.WORKERS_PER_DEVICE,
        dist=False,
        shuffle=False)

    # build the model and load checkpoint
    model = build_model(det_cfg.MODEL)

    checkpoint = load_checkpoint(model, model_path, map_location='cpu')

    if 'CLASSES' in checkpoint['meta']:
        model.CLASSES = checkpoint['meta']['CLASSES']
    else:
        model.CLASSES = dataset.CLASSES

    model = DataParallel(model, device_ids=[0])
    outputs = det_single_device_test(
        model, data_loader, score_thr=score_thr)

    save_det_json(outputs, json_path)


def infer_labels(qry_outputs, ref_outputs, label_mapping, rank_list):
    """
    Get predicted labels

    Args:
        qry_outputs (dict): query outputs
        ref_outputs (dict): reference outputs
        label_mapping (dict): mapping from reference label to query label
    Returns:
        outputs: query bbox indices with assigned labels
    """

    ref_emb = ref_outputs['embeddings']
    ref_labels = ref_outputs['labels']

    qry_emb = qry_outputs['embeddings']
    qry_ids = qry_outputs['bbox_ids']

    dist_func = LpDistance()
    if torch.cuda.is_available():
        ref_emb = torch.from_numpy(ref_emb).cuda()
        qry_emb = torch.from_numpy(qry_emb).cuda()

        mat = dist_func(qry_emb, ref_emb)
        mat = mat.data.cpu().numpy()
    else:
        ref_emb = torch.from_numpy(ref_emb)
        qry_emb = torch.from_numpy(qry_emb)

        mat = dist_func(qry_emb, ref_emb)
        mat = mat.data.numpy()

    mat_inds = np.argsort(mat, axis=1)

    ref_labels = ref_labels.reshape((ref_labels.shape[0], ))
    pred_labels = ref_labels[mat_inds]

    result = {}
    result['bbox_ids'] = qry_ids

    for k in rank_list:
        print('Assign label by frequency from top {} predictions'.format(k))
        if k == 1:
            result['labels_top_{}'.format(k)] = pred_labels[:, 0]
            continue

        pred_labels_top_k = []
        for i in range(pred_labels.shape[0]):
            top_k = pred_labels[i, :k]
            votes = Counter(top_k)
            pred = votes.most_common(1)[0][0]
            # convert to query label
            pred = label_mapping[pred]
            pred_labels_top_k.append(pred)

        result['labels_top_{}'.format(k)] = np.array(pred_labels_top_k)

    return result


def save_submit_json(outputs, det_json_path, out_json_path, score_thr=0.1, top_k=1):
    bbox_ids = outputs['bbox_ids']
    labels = outputs['labels_top_{}'.format(top_k)]
    pred_dict = {}
    for i, bbox_id in enumerate(bbox_ids):
        pred_dict[bbox_id] = labels[i]

    with open(det_json_path, 'r') as f:
        data_ori = json.load(f)

    ann_new = []
    for ann in data_ori['annotations']:
        assert ann['id'] in pred_dict
        if ann['score'] < score_thr:
            continue

        label = pred_dict[ann['id']]
        ann['category_id'] = int(label)
        ann_new.append(ann)

    data_ori['annotations'] = ann_new

    # out_json_path = out_json_path.replace('.json', '_top_{}.json'.format(top_k))
    with open(out_json_path, 'w') as wf:
        json.dump(data_ori, wf)
        print('Results have been saved at ' + out_json_path)


def run_emb_task(cfg_path, model_path, det_json_path,
                 out_json_path, label_mapping, score_thr, top_k_list=[1]):
    print('Running embedding task ...')
    emb_cfg = cfg.clone()
    get_task_cfg(emb_cfg, cfg_path)

    # build the dataloader
    dataset_args = get_dataset_global_args(emb_cfg.DATA)

    dataset_ref = build_dataset(
        emb_cfg.DATA.VAL_DATA, emb_cfg.DATA.TEST_TRANSFORMS, dataset_args)
    data_loader_ref = build_dataloader(
        dataset_ref,
        samples_per_device=32,  # emb_cfg.DATA.VAL_DATA.SAMPLES_PER_DEVICE,
        workers_per_device=WORKERS_PER_DEVICE,  # emb_cfg.DATA.VAL_DATA.WORKERS_PER_DEVICE,
        dist=False,
        shuffle=False)

    # build the model and load checkpoint
    model = build_model(emb_cfg.MODEL)

    checkpoint = load_checkpoint(model, model_path, map_location='cpu')

    if 'CLASSES' in checkpoint['meta']:
        model.CLASSES = checkpoint['meta']['CLASSES']
    else:
        model.CLASSES = dataset_ref.CLASSES

    model = DataParallel(model, device_ids=[0])
    print('(1/3) computing reference embeddings ...')
    outputs_ref = emb_single_device_test(
        model, data_loader_ref, with_label=True)

    dataset_qry = build_dataset(
        emb_cfg.DATA.TEST_DATA, emb_cfg.DATA.TEST_TRANSFORMS, dataset_args)
    data_loader_qry = build_dataloader(
        dataset_qry,
        samples_per_device=32,  # emb_cfg.DATA.TEST_DATA.SAMPLES_PER_DEVICE,
        workers_per_device=WORKERS_PER_DEVICE,  # emb_cfg.DATA.TEST_DATA.WORKERS_PER_DEVICE,
        dist=False,
        shuffle=False)

    print('(2/3) computing query embeddings ...')
    outputs_qry = emb_single_device_test(
        model, data_loader_qry, with_label=False)

    print('(3/3) inferring query labels ...')
    outputs = infer_labels(outputs_qry, outputs_ref, label_mapping, top_k_list)

    save_submit_json(
        outputs, det_json_path, out_json_path, score_thr, top_k=1)


def get_label_mapping(ref_json_path, qry_json_path):

    with open(ref_json_path, 'r') as f:
        ref_data = json.load(f)

    with open(qry_json_path, 'r') as f:
        qry_data = json.load(f)

    ref_cat_list = ref_data['categories']
    qry_cat_list = qry_data['categories']

    qry_dict = {}
    for qry_cat in qry_cat_list:
        cid = int(qry_cat['id'])
        name = qry_cat['name']
        qry_dict[name] = cid

    mapping_dict = {}
    for ref_cat in ref_cat_list:
        cid = int(ref_cat['id'])
        name = ref_cat['name']
        if name in qry_dict:
            mapping_dict[cid] = qry_dict[name]
        else:
            mapping_dict[cid] = 0  # TODO: how to deal with this?

    return mapping_dict


def run():
    mvt_path = Path(MVT_ROOT)
    det_cfg_path = mvt_path / 'model/tasks/detections/det_yolov4_9a_retail_one.yaml'
    det_model_path = mvt_path / 'model_files/det_yolov4_9a_retail_one/epoch_200.pth'

    det_json_path = mvt_path / 'data/test/a_det_annotations.json'
    det_score_thr = 0.1

    run_det_task(str(det_cfg_path), str(det_model_path),
                 str(det_json_path), det_score_thr)

    ref_json_path = mvt_path / 'data/test/b_annotations.json'
    qry_json_path = mvt_path / 'data/test/a_annotations.json'
    label_mapping = get_label_mapping(ref_json_path, qry_json_path)

    emb_cfg_path = mvt_path / 'model/tasks/embeddings/emb_resnet50_mlp_loc_retail.yaml'
    emb_model_path = mvt_path / 'model_files/emb_resnet50_mlp_loc_retail/epoch_50.pth'
    out_json_path = mvt_path / 'submit/output.json'

    run_emb_task(
        str(emb_cfg_path), str(emb_model_path),
        str(det_json_path), str(out_json_path),
        label_mapping=label_mapping,
        score_thr=det_score_thr, top_k_list=[1])


if __name__ == '__main__':
    run()
