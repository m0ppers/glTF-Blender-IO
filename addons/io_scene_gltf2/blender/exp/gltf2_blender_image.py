# Copyright 2018-2019 The glTF-Blender-IO authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import bpy
import os
from typing import Optional
import numpy as np
import tempfile
import enum


class Channel(enum.IntEnum):
    R = 0
    G = 1
    B = 2
    A = 3

# These describe how an ExportImage's channels should be filled.

class FillImage:
    """Fills a channel with the channel src_chan from a Blender image."""
    def __init__(self, image: bpy.types.Image, src_chan: Channel):
        self.image = image
        self.src_chan = src_chan

class FillWhite:
    """Fills a channel with all ones (1.0)."""
    pass


class ExportImage:
    """Custom image class.

    An image is represented by giving a description of how to fill its red,
    green, blue, and alpha channels. For example:

        self.fills = {
            Channel.R: FillImage(image=bpy.data.images['Im1'], src_chan=Channel.B),
            Channel.G: FillWhite(),
        }

    This says that the ExportImage's R channel should be filled with the B
    channel of the Blender image 'Im1', and the ExportImage's G channel
    should be filled with all 1.0s. Undefined channels mean we don't care
    what values that channel has.

    This is flexible enough to handle the case where eg. the user used the R
    channel of one image as the metallic value and the G channel of another
    image as the roughness, and we need to synthesize an ExportImage that
    packs those into the B and G channels for glTF.

    Storing this description (instead of raw pixels) lets us make more
    intelligent decisions about how to encode the image.
    """

    def __init__(self):
        self.fills = {}

    @staticmethod
    def from_blender_image(image: bpy.types.Image):
        export_image = ExportImage()
        for chan in range(image.channels):
            export_image.fill_image(image, dst_chan=chan, src_chan=chan)
        return export_image

    def fill_image(self, image: bpy.types.Image, dst_chan: Channel, src_chan: Channel):
        self.fills[dst_chan] = FillImage(image, src_chan)

    def fill_white(self, dst_chan: Channel):
        self.fills[dst_chan] = FillWhite()

    def is_filled(self, chan: Channel) -> bool:
        return chan in self.fills

    def __on_happy_path(self) -> bool:
        # Whether there is an existing Blender image we can use for this
        # ExportImage because all the channels come from the matching
        # channel of that image, eg.
        #
        #     self.fills = {
        #         Channel.R: FillImage(image=im, src_chan=Channel.R),
        #         Channel.G: FillImage(image=im, src_chan=Channel.G),
        #     }
        return (
            all(isinstance(fill, FillImage) for fill in self.fills.values()) and
            all(dst_chan == fill.src_chan for dst_chan, fill in self.fills.items()) and
            len(set(fill.image.name for fill in self.fills.values())) == 1
        )

    def encode(self, mime_type: Optional[str]) -> bytes:
        self.mime_type = mime_type

        # Happy path = we can just use an existing Blender image
        if self.__on_happy_path():
            return self.__encode_happy()

        # Unhappy path = we need to create the image self.fills describes.
        return self.__encode_unhappy()

    def __encode_happy(self) -> bytes:
        for fill in self.fills.values():
            return self.__encode_from_image(fill.image)

    def __encode_unhappy(self) -> bytes:
        tmp_scene = None
        orig_colorspaces = {}
        try:
            # Create a Compositor nodetree on a temp scene that will
            # construct the image described in self.fills. Think like...
            #
            #     [ Image ]--->[ Sep RGBA ]    [ Comb RGBA ]
            #                  [  src_chan]--->[dst_chan   ]--->[ Output ]
            #
            # Then render the scene to a temp file and read it back. This is
            # pretty hacky, but it's faster than doing this by manipulating
            # pixels, at least for big images.
            tmp_scene = bpy.data.scenes.new('##gltf-export:tmp-scene##')

            tmp_scene.use_nodes = True
            node_tree = tmp_scene.node_tree
            for node in node_tree.nodes:
                node_tree.nodes.remove(node)

            size = None

            out = node_tree.nodes.new('CompositorNodeComposite')
            comb_rgba = node_tree.nodes.new('CompositorNodeCombRGBA')
            comb_rgba.inputs[0].default_value = 1.0
            comb_rgba.inputs[1].default_value = 1.0
            comb_rgba.inputs[2].default_value = 1.0
            comb_rgba.inputs[3].default_value = 1.0
            node_tree.links.new(out.inputs['Image'], comb_rgba.outputs['Image'])

            for dst_chan, fill in self.fills.items():
                if isinstance(fill, FillImage):
                    img = node_tree.nodes.new('CompositorNodeImage')
                    img.image = fill.image
                    sep_rgba = node_tree.nodes.new('CompositorNodeSepRGBA')
                    node_tree.links.new(sep_rgba.inputs['Image'], img.outputs['Image'])
                    node_tree.links.new(comb_rgba.inputs[dst_chan], sep_rgba.outputs[fill.src_chan])

                    # Use Non-Color colorspace for all images and set the
                    # display colorspace to 'None' below when we render.
                    if fill.image.name not in orig_colorspaces:
                        # Save the original value so we can put it back.
                        orig_colorspace = fill.image.colorspace_settings.name
                        orig_colorspaces[fill.image.name] = orig_colorspace
                    fill.image.colorspace_settings.name = 'Non-Color'

                    if size is None:
                        size = (fill.image.size[0], fill.image.size[1])
                    else:
                        # All images should be the same size (should be
                        # guaranteed by gather_texture_info)
                        assert size == (fill.image.size[0], fill.image.size[1])

            if size is None:
                size = (1, 1)

            return _render_tmp_scene(
                tmp_scene=tmp_scene,
                width=size[0],
                height=size[1],
                mime_type=self.mime_type,
                colorspace='None',
                has_alpha=Channel.A in self.fills,
            )

        finally:
            if tmp_scene is not None:
                bpy.data.scenes.remove(tmp_scene, do_unlink=True)

            # Restore original colorspace settings
            for img_name, colorspace in orig_colorspaces.items():
                bpy.data.images[img_name].colorspace_settings.name = colorspace

    def __encode_from_image(self, image: bpy.types.Image) -> bytes:
        file_format = {
            "image/jpeg": "JPEG",
            "image/png": "PNG"
        }.get(self.mime_type, "PNG")

        # For images already on disk, skip saving them and just read them back.
        # (What about if the image has changed on disk though?)
        if file_format == image.file_format and not image.is_dirty:
            src_path = bpy.path.abspath(image.filepath_raw)
            if os.path.isfile(src_path):
                with open(src_path, 'rb') as f:
                    return f.read()

        with tempfile.TemporaryDirectory() as tmpdirname:
            tmpfilename = tmpdirname + "/img"

            # Save original values
            orig_filepath_raw = image.filepath_raw
            orig_file_format = image.file_format
            try:
                image.filepath_raw = tmpfilename
                image.file_format = file_format

                image.save()

            finally:
                # Restore original values
                image.filepath_raw = orig_filepath_raw
                image.file_format = orig_file_format

            with open(tmpfilename, "rb") as f:
                return f.read()


def _render_tmp_scene(
    tmp_scene, width, height, mime_type, colorspace, has_alpha,
) -> bytes:
    """Fill in render settings, render the scene to a file, and read back."""
    tmp_scene.render.resolution_x = width
    tmp_scene.render.resolution_y = height
    tmp_scene.render.resolution_percentage = 100

    if mime_type:
        tmp_scene.render.image_settings.file_format = {
            "image/jpeg": "JPEG",
            "image/png": "PNG",
        }.get(mime_type, "PNG")
    tmp_scene.display_settings.display_device = colorspace
    tmp_scene.render.image_settings.color_mode = 'RGBA' if has_alpha else 'RGB'
    tmp_scene.render.dither_intensity = 0.0

    # Turn off all metadata (stuff like use_stamp_date, etc.)
    for attr in dir(tmp_scene.render):
        if attr.startswith('use_stamp_'):
            setattr(tmp_scene.render, attr, False)

    with tempfile.TemporaryDirectory() as tmpdirname:
        tmpfilename = tmpdirname + "/img"
        tmp_scene.render.filepath = tmpfilename
        tmp_scene.render.use_file_extension = False

        bpy.ops.render.render(scene=tmp_scene.name, write_still=True)

        with open(tmpfilename, "rb") as f:
            return f.read()
