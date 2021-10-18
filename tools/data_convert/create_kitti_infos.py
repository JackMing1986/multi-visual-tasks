import argparse
from os import path as osp

from mvt.datasets.data_converter.kitti_converter import (
    create_kitti_info_file,
    create_reduced_point_cloud
)
from mvt.datasets.data_converter.create_gt_groundtruth import (
    create_kitti_groundtruth_database
)


def kitti_data_prep(root_path, info_prefix, version, out_dir):
    """Prepare data related to Kitti dataset.

    Related data consists of '.pkl' files recording basic infos,
    2D annotations and groundtruth database.

    Args:
        root_path (str): Path of dataset root.
        info_prefix (str): The prefix of info filenames.
        version (str): Dataset version.
        out_dir (str): Output directory of the groundtruth database info.
    """
    create_kitti_info_file(root_path, info_prefix)
    create_reduced_point_cloud(root_path, info_prefix)

    info_train_path = osp.join(root_path, f'{info_prefix}_infos_train.pkl')
    info_val_path = osp.join(root_path, f'{info_prefix}_infos_val.pkl')
    info_trainval_path = osp.join(root_path,
                                  f'{info_prefix}_infos_trainval.pkl')
    info_test_path = osp.join(root_path, f'{info_prefix}_infos_test.pkl')

    create_kitti_groundtruth_database(
        'KittiDataset',
        root_path,
        info_prefix,
        f'{out_dir}/{info_prefix}_infos_train.pkl',
        relative_path=False,
        mask_anno_path='instances_train.json',
        with_mask=(version == 'mask'))


def arg_parser():
    parser = argparse.ArgumentParser(description='Data converter arg parser')
    parser.add_argument('dataset', metavar='kitti', help='name of the dataset')
    parser.add_argument(
        '--root-path',
        type=str,
        default='./data/kitti',
        help='specify the root path of dataset')
    parser.add_argument(
        '--max-sweeps',
        type=int,
        default=10,
        required=False,
        help='specify sweeps of lidar per example')
    parser.add_argument(
        '--out-dir',
        type=str,
        default='./data/kitti',
        required='False',
        help='name of info pkl')
    parser.add_argument('--extra-tag', type=str, default='kitti')
    parser.add_argument(
        '--workers', type=int, default=4, help='number of threads to be used')
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = arg_parser()
    kitti_data_prep(
        root_path=args.root_path,
        info_prefix=args.extra_tag,
        version=args.version,
        out_dir=args.out_dir)