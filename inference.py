#!/usr/bin/env python3
# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# gradio demo
# --------------------------------------------------------
import argparse
import gradio
import os
import torch
import numpy as np
import tempfile
import functools
import trimesh
import copy
from scipy.spatial.transform import Rotation

from dust3r.inference import inference, load_model
from dust3r.image_pairs import make_pairs
from dust3r.utils.image import load_images, rgb
from dust3r.utils.device import to_numpy
from dust3r.viz import add_scene_cam, CAM_COLORS, OPENGL, pts3d_to_trimesh, cat_meshes
from dust3r.cloud_opt import global_aligner, GlobalAlignerMode
import open3d as o3d
import matplotlib.pyplot as pl
import json

pl.ion()

torch.backends.cuda.matmul.allow_tf32 = True  # for gpu >= Ampere and pytorch >= 1.12
batch_size = 1


def get_args_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--item",
        type=str,
        default="box",
        help="estimate 3D model from a list of images",
    )
    parser.add_argument(
        "--views",
        type=int,
        default=2,
        help="number of images to reconstruct the scene",
    )
    return parser


def _convert_scene_output_to_glb(
    outdir,
    scene_name,
    imgs,
    pts3d,
    mask,
    focals,
    cams2world,
    cam_size=0.05,
    cam_color=None,
    as_pointcloud=False,
    transparent_cams=False,
):
    assert len(pts3d) == len(mask) <= len(imgs) <= len(cams2world) == len(focals)
    pts3d = to_numpy(pts3d)
    imgs = to_numpy(imgs)
    focals = to_numpy(focals)
    cams2world = to_numpy(cams2world)

    scene = trimesh.Scene()

    # full pointcloud
    if as_pointcloud:
        pts = np.concatenate([p[m] for p, m in zip(pts3d, mask)])
        col = np.concatenate([p[m] for p, m in zip(imgs, mask)])
        pct = trimesh.PointCloud(pts.reshape(-1, 3), colors=col.reshape(-1, 3))
        scene.add_geometry(pct)
    else:
        meshes = []
        for i in range(len(imgs)):
            meshes.append(pts3d_to_trimesh(imgs[i], pts3d[i], mask[i]))
        mesh = trimesh.Trimesh(**cat_meshes(meshes))
        scene.add_geometry(mesh)

    # add each camera
    # for i, pose_c2w in enumerate(cams2world):
    #     if isinstance(cam_color, list):
    #         camera_edge_color = cam_color[i]
    #     else:
    #         camera_edge_color = cam_color or CAM_COLORS[i % len(CAM_COLORS)]
    #     add_scene_cam(
    #         scene,
    #         pose_c2w,
    #         camera_edge_color,
    #         None if transparent_cams else imgs[i],
    #         focals[i],
    #         imsize=imgs[i].shape[1::-1],
    #         screen_width=cam_size,
    #     )

    rot = np.eye(4)
    type = ""
    if as_pointcloud:
        type = "pointcloud"
    else:
        type = "mesh"
    rot[:3, :3] = Rotation.from_euler("y", np.deg2rad(180)).as_matrix()
    scene.apply_transform(np.linalg.inv(cams2world[0] @ OPENGL @ rot))
    outfile = os.path.join(outdir, f"{scene_name}_{type}.glb")
    print("(exporting 3D scene to", outfile, ")")
    scene.export(file_obj=outfile)
    return outfile


def get_3D_model_from_scene(
    outdir,
    scene_name,
    scene,
    min_conf_thr=3,
    as_pointcloud=False,
    mask_sky=False,
    clean_depth=False,
    transparent_cams=False,
    cam_size=0.05,
):
    """
    extract 3D_model (glb file) from a reconstructed scene
    """
    if scene is None:
        return None
    # post processes
    if clean_depth:
        scene = scene.clean_pointcloud()
    if mask_sky:
        scene = scene.mask_sky()

    # get optimized values from scene
    rgbimg = scene.imgs
    focals = scene.get_focals().cpu()
    cams2world = scene.get_im_poses().cpu()
    # 3D pointcloud from depthmap, poses and intrinsics
    pts3d = to_numpy(scene.get_pts3d())
    scene.min_conf_thr = float(scene.conf_trf(torch.tensor(min_conf_thr)))
    msk = to_numpy(scene.get_masks())
    return _convert_scene_output_to_glb(
        outdir,
        scene_name,
        rgbimg,
        pts3d,
        msk,
        focals,
        cams2world,
        as_pointcloud=as_pointcloud,
        transparent_cams=transparent_cams,
        cam_size=cam_size,
    )


def get_reconstructed_scene(
    outdir,
    model,
    device,
    image_size,
    filelist,
    schedule,
    niter,
    min_conf_thr,
    as_pointcloud,
    mask_sky,
    clean_depth,
    transparent_cams,
    cam_size,
    scenegraph_type,
    winsize,
    refid,
):
    """
    from a list of images, run dust3r inference, global aligner.
    then run get_3D_model_from_scene
    """
    imgs = load_images(filelist, size=image_size)
    if len(imgs) == 1:
        imgs = [imgs[0], copy.deepcopy(imgs[0])]
        imgs[1]["idx"] = 1
    if scenegraph_type == "swin":
        scenegraph_type = scenegraph_type + "-" + str(winsize)
    elif scenegraph_type == "oneref":
        scenegraph_type = scenegraph_type + "-" + str(refid)

    pairs = make_pairs(
        imgs, scene_graph=scenegraph_type, prefilter=None, symmetrize=True
    )
    output = inference(pairs, model, device, batch_size=batch_size)

    mode = (
        GlobalAlignerMode.PointCloudOptimizer
        if len(imgs) > 2
        else GlobalAlignerMode.PairViewer
    )
    scene = global_aligner(output, device=device, mode=mode)
    lr = 0.01

    if mode == GlobalAlignerMode.PointCloudOptimizer:
        loss = scene.compute_global_alignment(
            init="mst", niter=niter, schedule=schedule, lr=lr
        )

    outfile = get_3D_model_from_scene(
        outdir,
        scene,
        min_conf_thr,
        as_pointcloud,
        mask_sky,
        clean_depth,
        transparent_cams,
        cam_size,
    )

    # also return rgb, depth and confidence imgs
    # depth is normalized with the max value for all images
    # we apply the jet colormap on the confidence maps
    rgbimg = scene.imgs
    depths = to_numpy(scene.get_depthmaps())
    confs = to_numpy([c for c in scene.im_conf])
    cmap = pl.get_cmap("jet")
    depths_max = max([d.max() for d in depths])
    depths = [d / depths_max for d in depths]
    confs_max = max([d.max() for d in confs])
    confs = [cmap(d / confs_max) for d in confs]

    imgs = []
    for i in range(len(rgbimg)):
        imgs.append(rgbimg[i])
        imgs.append(rgb(depths[i]))
        imgs.append(rgb(confs[i]))

    return scene, outfile, imgs


def set_scenegraph_options(inputfiles, winsize, refid, scenegraph_type):
    num_files = len(inputfiles) if inputfiles is not None else 1
    max_winsize = max(1, (num_files - 1) // 2)
    if scenegraph_type == "swin":
        winsize = gradio.Slider(
            label="Scene Graph: Window Size",
            value=max_winsize,
            minimum=1,
            maximum=max_winsize,
            step=1,
            visible=True,
        )
        refid = gradio.Slider(
            label="Scene Graph: Id",
            value=0,
            minimum=0,
            maximum=num_files - 1,
            step=1,
            visible=False,
        )
    elif scenegraph_type == "oneref":
        winsize = gradio.Slider(
            label="Scene Graph: Window Size",
            value=max_winsize,
            minimum=1,
            maximum=max_winsize,
            step=1,
            visible=False,
        )
        refid = gradio.Slider(
            label="Scene Graph: Id",
            value=0,
            minimum=0,
            maximum=num_files - 1,
            step=1,
            visible=True,
        )
    else:
        winsize = gradio.Slider(
            label="Scene Graph: Window Size",
            value=max_winsize,
            minimum=1,
            maximum=max_winsize,
            step=1,
            visible=False,
        )
        refid = gradio.Slider(
            label="Scene Graph: Id",
            value=0,
            minimum=0,
            maximum=num_files - 1,
            step=1,
            visible=False,
        )
    return winsize, refid


def main_demo(tmpdirname, model, device, image_size, server_name, server_port):
    recon_fun = functools.partial(
        get_reconstructed_scene, tmpdirname, model, device, image_size
    )
    model_from_scene_fun = functools.partial(get_3D_model_from_scene, tmpdirname)
    with gradio.Blocks(
        css=""".gradio-container {margin: 0 !important; min-width: 100%};""",
        title="DUSt3R Demo",
    ) as demo:
        # scene state is save so that you can change conf_thr, cam_size... without rerunning the inference
        scene = gradio.State(None)
        gradio.HTML('<h2 style="text-align: center;">DUSt3R Demo</h2>')
        with gradio.Column():
            inputfiles = gradio.File(file_count="multiple")
            with gradio.Row():
                schedule = gradio.Dropdown(
                    ["linear", "cosine"],
                    value="linear",
                    label="schedule",
                    info="For global alignment!",
                )
                niter = gradio.Number(
                    value=300,
                    precision=0,
                    minimum=0,
                    maximum=5000,
                    label="num_iterations",
                    info="For global alignment!",
                )
                scenegraph_type = gradio.Dropdown(
                    ["complete", "swin", "oneref"],
                    value="complete",
                    label="Scenegraph",
                    info="Define how to make pairs",
                    interactive=True,
                )
                winsize = gradio.Slider(
                    label="Scene Graph: Window Size",
                    value=1,
                    minimum=1,
                    maximum=1,
                    step=1,
                    visible=False,
                )
                refid = gradio.Slider(
                    label="Scene Graph: Id",
                    value=0,
                    minimum=0,
                    maximum=0,
                    step=1,
                    visible=False,
                )

            run_btn = gradio.Button("Run")

            with gradio.Row():
                # adjust the confidence threshold
                min_conf_thr = gradio.Slider(
                    label="min_conf_thr", value=3.0, minimum=1.0, maximum=20, step=0.1
                )
                # adjust the camera size in the output pointcloud
                cam_size = gradio.Slider(
                    label="cam_size", value=0.05, minimum=0.001, maximum=0.1, step=0.001
                )
            with gradio.Row():
                as_pointcloud = gradio.Checkbox(value=False, label="As pointcloud")
                # two post process implemented
                mask_sky = gradio.Checkbox(value=False, label="Mask sky")
                clean_depth = gradio.Checkbox(value=True, label="Clean-up depthmaps")
                transparent_cams = gradio.Checkbox(
                    value=False, label="Transparent cameras"
                )

            outmodel = gradio.Model3D()
            outgallery = gradio.Gallery(
                label="rgb,depth,confidence", columns=3, height="100%"
            )

            # events
            scenegraph_type.change(
                set_scenegraph_options,
                inputs=[inputfiles, winsize, refid, scenegraph_type],
                outputs=[winsize, refid],
            )
            inputfiles.change(
                set_scenegraph_options,
                inputs=[inputfiles, winsize, refid, scenegraph_type],
                outputs=[winsize, refid],
            )
            run_btn.click(
                fn=recon_fun,
                inputs=[
                    inputfiles,
                    schedule,
                    niter,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                    scenegraph_type,
                    winsize,
                    refid,
                ],
                outputs=[scene, outmodel, outgallery],
            )
            min_conf_thr.release(
                fn=model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
            cam_size.change(
                fn=model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
            as_pointcloud.change(
                fn=model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
            mask_sky.change(
                fn=model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
            clean_depth.change(
                fn=model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
            transparent_cams.change(
                model_from_scene_fun,
                inputs=[
                    scene,
                    min_conf_thr,
                    as_pointcloud,
                    mask_sky,
                    clean_depth,
                    transparent_cams,
                    cam_size,
                ],
                outputs=outmodel,
            )
    demo.launch(share=False, server_name=server_name, server_port=server_port)


def save_camera_poses(poses: torch.tensor, file_names: list, path: str) -> None:
    # convert pose to nparray
    M_ext = dict()
    assert len(file_names) == len(poses)
    for pose, view_name in zip(poses, file_names):
        image_file = f"{view_name}"
        M_ext[view_name] = pose.tolist()
    file_path = os.path.join("./estimation", path)
    if not os.path.isfile(file_path):
        os.mknod(file_path)
    with open(file_path, "w") as f:
        format_M_ext = {"extrinsics": M_ext}
        json.dump(format_M_ext, f)


def save_point_to_ply(points: np.ndarray, file_name: str) -> None:
    _, dim = points.shape
    if dim == 4:
        points = points[:, :3]
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(points)
    o3d.io.write_point_cloud(os.path.join("./estimation/", file_name), pcd)


if __name__ == "__main__":
    parser = get_args_parser()
    args = parser.parse_args()

    model_path = "checkpoints/DUSt3R_ViTLarge_BaseDecoder_512_dpt.pth"
    device = "cuda"
    batch_size = 1
    schedule = "cosine"
    lr = 0.01
    niter = 300

    model = load_model(model_path, device)
    # load_images can take a list of images or a directory
    # load images fom sfm_data/resize_image
    files_path = []
    frame_name = []

    num_view = args.views
    item = args.item
    resize_image_path = f"sfm_data/{item}_images"

    for filename in os.listdir(f"sfm_data/{item}_images"):
        if len(files_path) == num_view:
            break
        files_path.append(os.path.join(resize_image_path, filename))
        frame_name.append(filename)

    # use the first two images to reconstruct the scene
    images = load_images(files_path[:num_view], size=512)
    pairs = make_pairs(images, scene_graph="complete", prefilter=None, symmetrize=True)
    output = inference(pairs, model, device, batch_size=batch_size)

    scene = global_aligner(
        output, device=device, mode=GlobalAlignerMode.PointCloudOptimizer
    )
    loss = scene.compute_global_alignment(
        init="mst", niter=niter, schedule=schedule, lr=lr
    )

    # retrieve useful values from scene:
    imgs = scene.imgs
    focals = scene.get_focals()
    poses = scene.get_im_poses()
    pts3d = scene.get_pts3d()
    confidence_masks = scene.get_masks()

    # save mesh,pointcloud and camera poses to local
    get_3D_model_from_scene(f"./estimation/", item, scene)
    get_3D_model_from_scene(f"./estimation/", item, scene, as_pointcloud=True)
    save_camera_poses(poses, frame_name, f"{item}_poses.json")
    point_cloud = torch.cat(pts3d, dim=0).reshape(-1, 3).detach().cpu().numpy()
    save_point_to_ply(
        point_cloud,
        f"{item}_point_cloud.ply",
    )
    # visualize reconstruction
    # scene.show()
