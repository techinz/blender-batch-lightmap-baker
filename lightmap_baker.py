# Blender Script for batch lightmap baking
# This script allows you to bake lightmaps for multiple objects in Blender
# Read how to use it in the README file: https://github.com/techinz/blender-batch-lightmap-baker/README.md
# By techinz

import logging
import os
from dataclasses import dataclass
from typing import Optional, Set, Literal, Any

import bpy

NODE_BAKE_IMAGE_NAME = "BakeImage"  # name of the image node used for baking
DEFAULT_IMAGE_SIZE = 1024  # default image size for baking
DEFAULT_SAMPLES = 1024  # default number of samples for baking
DEFAULT_MARGIN = 16  # default margin for baking
DEFAULT_BAKING_DIR = "baked_textures"  # default output directory for baked textures


# --- DATACLASSES ---
@dataclass
class NodeTypes:
    """
    Dataclass for storing node types.
    In Blender's node system, when searching nodes, it requires the short type name (e.g. 'BSDF_PRINCIPLED').
    And when creating nodes, it requires the full internal node identifier name (e.g 'ShaderNodeBsdfPrincipled').
    """

    # for searching
    OUTPUT_MATERIAL = 'OUTPUT_MATERIAL'  # material output node type
    BSDF_PRINCIPLED = 'BSDF_PRINCIPLED'  # default fallback shader node type (default fallback node type when the stored original one not found)
    TEX_IMAGE = 'TEX_IMAGE'  # image texture node type (for baked images)

    # for creating
    OUTPUT_MATERIAL_CREATE = 'ShaderNodeOutputMaterial'
    BSDF_PRINCIPLED_CREATE = 'ShaderNodeBsdfPrincipled'
    TEX_IMAGE_CREATE = 'ShaderNodeTexImage'


@dataclass
class SearchNodeData:
    """
    Data class for searching nodes in the material node tree.
    At least one of type, name or custom_property must be specified.

    If custom_property is not found - the node with the specified fallback type will be searched for.

    If create_if_not_found is set - a new node of the specified type and name will be created if not found.

    :param type: type of the node to search for (e.g. NodeTypes.TEX_IMAGE)
    :param name: name of the node to search for (e.g. 'BakeImage')
    :param custom_property: name of the custom property to search for (e.g. 'originally_connected_to_output_surface')
    :param custom_property_value: value of the custom property to search for (e.g. 'originally_connected_to_output_surface'=True)
    :param custom_property_not_found_fallback_type: type of the node to search for if custom property is not found (e.g. NodeTypes.BSDF_PRINCIPLED)
    :param socket_index: index of the input/output socket to connect (0 for the first socket, 1 for the second, etc.)
    :param create_if_not_found: if True - create a new node of the specified type and name (optional) if not found
    :param create_type: type of the node to create if not found (e.g. NodeTypes.TEX_IMAGE)
    :param create_name: (optional) name of the node to create if not found (e.g. 'BakeImage')
    """

    type: Optional[str] = None

    name: Optional[str] = None

    custom_property: Optional[str] = None
    custom_property_value: Optional[Any] = True
    custom_property_not_found_fallback_type: Optional[str] = None

    socket_index: int = 0

    create_if_not_found: bool = False
    # at least one of create_type or create_name must be specified if create_if_not_found is True
    create_type: Optional[str] = None
    create_name: Optional[str] = None

    def __post_init__(self):
        # validate the data
        if not any((self.type, self.name, self.custom_property)):
            raise ValueError("At least one of type, name or custom_property must be specified")

        # create_if_not_found is set but no needed data specified
        if self.create_if_not_found is True and not self.create_type:
            raise ValueError("create_if_not_found is set but no create_type specified")


# --- OPERATORS ---
class MessageBoxOperator(bpy.types.Operator):
    bl_idname = "message.messagebox"
    bl_label = "Batch Lightmap Baker"

    message: bpy.props.StringProperty(default="")
    icon: bpy.props.StringProperty(default='INFO')

    def execute(self, context: bpy.types.Context):
        return {'FINISHED'}

    def invoke(self, context, event: Any):
        wm = context.window_manager
        return wm.invoke_popup(self, width=400)

    def draw(self, context: bpy.types.Context):
        self.layout.label(text=self.message, icon=self.icon)
        self.layout.separator()


class BakeAllObjectsOperator(bpy.types.Operator):
    bl_idname = "object.bake_all_objects"
    bl_label = "Bake All Objects"

    def execute(self, context: bpy.types.Context):
        settings = context.scene.bake_settings
        object_names = context.scene.bake_settings.get_object_names()
        shading_manager = ShadingManager(self)

        # progress reporting because baking can take a while
        context.window_manager.progress_begin(0, len(object_names))
        for i, name in enumerate(object_names):
            context.window_manager.progress_update(i)
            shading_manager.bake_object_light(name, settings)
        context.window_manager.progress_end()

        return {'FINISHED'}


class SwitchToRealShadingOperator(bpy.types.Operator):
    bl_idname = "object.switch_to_real_shading"
    bl_label = "Switch to Real Shading"

    def execute(self, context: bpy.types.Context):
        object_names = context.scene.bake_settings.get_object_names()
        shading_manager = ShadingManager(self)
        for name in object_names:
            shading_manager.switch_object_to_real_shading(name)
        return {'FINISHED'}


class SwitchToBakedShadingOperator(bpy.types.Operator):
    bl_idname = "object.switch_to_baked_shading"
    bl_label = "Switch to Baked Shading"

    def execute(self, context: bpy.types.Context):
        object_names = context.scene.bake_settings.get_object_names()
        shading_manager = ShadingManager(self)
        for name in object_names:
            shading_manager.switch_object_to_baked_shading(name)
        return {'FINISHED'}


# --- PROPERTIES (SETTINGS) ---
class BakeSettings(bpy.types.PropertyGroup):
    object_names: bpy.props.StringProperty(
        name="Object Names",
        description="Comma-separated list of object (mesh) names to bake",
        default="Floor, Ceiling"
    )
    bake_type: bpy.props.EnumProperty(
        name="Bake Type",
        description="Type of bake to perform",
        items=[
            ('COMBINED', "Combined", ""),
            ('DIFFUSE', "Diffuse", ""),
            ('GLOSSY', "Glossy", ""),
        ],
        default='COMBINED'
    )
    image_size: bpy.props.IntProperty(
        name="Image Size",
        description="Size of the baked texture",
        default=DEFAULT_IMAGE_SIZE,
        min=256,
        max=8192
    )
    samples: bpy.props.IntProperty(
        name="Samples",
        description="Number of Cycles samples for baking",
        default=DEFAULT_SAMPLES,
        min=1,
        max=8192
    )
    margin: bpy.props.IntProperty(
        name="Margin",
        description="Margin for baking",
        default=DEFAULT_MARGIN,
        min=0,
        max=64
    )
    output_dir: bpy.props.StringProperty(
        name="Output Directory",
        description="Directory to save baked textures",
        default=DEFAULT_BAKING_DIR,
        subtype='DIR_PATH'
    )

    def get_object_names(self):
        """
        Returns a list of object names from the input string.
        :return: list of object names
        """
        return [name.strip() for name in self.object_names.split(",") if name.strip()]


# --- UI ---
class BakePanel(bpy.types.Panel):
    bl_label = "Batch Lightmap Baking"
    bl_idname = "VIEW3D_PT_lightmap_baking"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Batch Lightmap Baker"

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        settings = context.scene.bake_settings

        # Objects section
        box = layout.box()
        box.label(text="Objects", icon='OUTLINER_OB_MESH')
        box.prop(settings, "object_names")

        # Baking parameters section
        box = layout.box()
        box.label(text="Baking Settings", icon='RENDER_STILL')
        box.prop(settings, "bake_type")
        row = box.row(align=True)
        row.prop(settings, "image_size")
        row.prop(settings, "margin")
        box.prop(settings, "samples")
        box.prop(settings, "output_dir")

        # Operations section
        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator("object.bake_all_objects", icon='RENDER_STILL')
        row = col.row()
        row.operator("object.switch_to_real_shading", icon='MATERIAL')
        row.operator("object.switch_to_baked_shading", icon='TEXTURE')


# --- SHADING MANAGER ---
class ShadingManager:
    def __init__(self, operator: Optional[bpy.types.Operator] = None):
        self.operator = operator

    def report_message(self, message_type: Set[Literal['INFO', 'WARNING', 'ERROR']], message: str,
                       show_popup: bool = False):
        """
        Reports (shows) a message to the user.
        :param message_type: {'INFO'}/{'WARNING'}/{'ERROR'}
        :param message: message to show
        :param show_popup: if True - show a popup message
        :return:
        """

        if self.operator:
            self.operator.report(message_type, message)
        else:
            logging.info(message)

        if show_popup:
            icon = 'INFO'
            if 'ERROR' in message_type:
                icon = 'ERROR'
            elif 'WARNING' in message_type:
                icon = 'CANCEL'

            bpy.ops.message.messagebox('INVOKE_DEFAULT', message=message, icon=icon)

    def bake_object_light(self, name: str, settings: BakeSettings):
        """
        Bakes the lightmap for the given object.
        :param name: object name
        :param settings: bake settings from the UI
        :return:
        """

        # create output directory if it doesn't exist
        try:
            if not os.path.exists(settings.output_dir):
                os.makedirs(settings.output_dir)
        except Exception as e:
            self.report_message({'ERROR'}, f"Failed to create directory: {str(e)}", show_popup=True)
            return

        # apply baking settings
        scene = bpy.context.scene
        scene.render.engine = 'CYCLES'  # baking works only with CYCLES
        scene.cycles.samples = settings.samples

        obj = bpy.data.objects.get(name)
        if not Utils.is_valid_mesh(obj):
            self.report_message({'WARNING'}, f"{name}: skipped (not found or not mesh)", show_popup=True)
            return

        self.report_message({'INFO'}, f"{name}: baking object")

        Utils.select_object(obj)  # select the object and set it as active

        # ensure UV map exists and if not - create one (with smart project)
        if not obj.data.uv_layers:  # no UV map found
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.uv.smart_project()
                bpy.ops.object.mode_set(mode='OBJECT')
                self.report_message({'INFO'}, f'{name}: new UV created')
            except Exception as e:
                self.report_message({'ERROR'}, f"{name}: failed to create UV map - {str(e)}", show_popup=True)
                return

        # create (or re-use) new image for baking
        img_name = f"{obj.name}_Baked"
        img = bpy.data.images.get(img_name)
        if not img:
            img = bpy.data.images.new(img_name, width=settings.image_size, height=settings.image_size,
                                      alpha=False)  # ensure to have no alpha for COMBINED

        # set up material (create if none)
        if not obj.data.materials:
            mat = bpy.data.materials.new(name=f"{obj.name}_BakingMat")
            obj.data.materials.append(mat)
            self.report_message({'INFO'}, f'{name}: new material created')

        self.report_message({'INFO'}, f'{name}: found {len(obj.data.materials)} materials')

        # switch to real shading before baking
        self.switch_object_to_real_shading(name)

        # create baked image texture node for every material of the object (with the same image path so it's baked in the same image)
        for mat in obj.data.materials:
            mat.use_nodes = True  # enable nodes
            nodes = mat.node_tree.nodes

            # remove old bake node
            for n in nodes:
                if n.name == NODE_BAKE_IMAGE_NAME:
                    nodes.remove(n)
                    break

            # create new bake node
            bake_node = nodes.new(type=NodeTypes.TEX_IMAGE_CREATE)
            bake_node.name = NODE_BAKE_IMAGE_NAME
            bake_node.image = img
            nodes.active = bake_node  # set the just created image node as active for this material

        # apply baking settings
        scene.render.bake.use_clear = True  # clear the image before baking
        scene.render.bake.margin = settings.margin  # margin for baking
        scene.render.bake.target = 'IMAGE_TEXTURES'
        scene.render.bake.use_selected_to_active = False

        # bake
        try:
            bpy.ops.object.bake(type=settings.bake_type)
        except Exception as e:
            self.report_message({'ERROR'}, f"{name}: baking failed - {str(e)}", show_popup=True)
            return

        # switch to baked shading after baking
        self.switch_object_to_baked_shading(name)

        # save baked image
        try:
            img.filepath_raw = os.path.abspath(os.path.join(settings.output_dir, f"{img_name}.png"))
            img.file_format = 'PNG'
            img.save()
        except Exception as e:
            self.report_message({'ERROR'}, f"{name}: failed to save image - {str(e)}", show_popup=True)

        self.report_message({'INFO'}, f"{name}: baked image saved to {settings.output_dir}/{img_name}.png")

    # switch to real shading (connect bsdf node to material output)
    def switch_object_to_real_shading(self, name: str):
        """
        Switches the object to real shading (connects originally connected node to material output's surface socket).
        :param name: object name
        :return:
        """

        obj = bpy.data.objects.get(name)
        if not Utils.is_valid_mesh(obj):
            self.report_message({'WARNING'}, f"{name}: skipped (not found or not mesh)", show_popup=True)
            return

        self.report_message({'INFO'}, f"{name}: switching to real shading")

        Utils.select_object(obj)  # select the object and set it as active

        if not obj.data.materials:
            return self.report_message({'WARNING'}, f'{name}: materials not found', show_popup=True)

        # get the original node that was connected to the material output node's surface socket
        # before switching to baked shading by searching for the custom property originally_connected_to_output_surface=True
        # or if not found - find a node with the fallback type (e.g. Principled BSDF)
        node_a_data = SearchNodeData(
            custom_property='originally_connected_to_output_surface',
            custom_property_value=True,
            custom_property_not_found_fallback_type=NodeTypes.BSDF_PRINCIPLED,
        )
        node_b_data = SearchNodeData(
            type=NodeTypes.OUTPUT_MATERIAL,

            create_if_not_found=True,
            create_type=NodeTypes.OUTPUT_MATERIAL_CREATE,
        )
        success = Utils.connect_nodes(obj, node_a_data, node_b_data,
                                      self.report_message)  # connect nodes for all materials
        if not success:
            return self.report_message({'WARNING'}, f'{name}: failed to connect nodes', show_popup=True)

        # reset the custom property originally_connected_to_output_surface=True
        for mat in obj.data.materials:
            connected_to_output_surface_node = Utils.find_node(mat.node_tree.nodes, SearchNodeData(
                custom_property='originally_connected_to_output_surface',
                custom_property_value=True,
            ))
            if connected_to_output_surface_node:
                connected_to_output_surface_node["originally_connected_to_output_surface"] = None
                self.report_message({'INFO'}, f'{name}: custom property removed from the original node')

        self.report_message({'INFO'}, f'{name}: switched to real shading')

    # switch to baked shading (connect baked image node to material output)
    def switch_object_to_baked_shading(self, name: str):
        """
        Switches the object to baked shading (connects baked image node to material output).
        :param name: object name
        :return:
        """

        obj = bpy.data.objects.get(name)
        if not Utils.is_valid_mesh(obj):
            self.report_message({'WARNING'}, f"{name}: skipped (not found or not mesh)", show_popup=True)
            return

        self.report_message({'INFO'}, f"{name}: switching to baked shading")

        Utils.select_object(obj)  # select the object and set it as active

        if not obj.data.materials:
            return self.report_message({'WARNING'}, f'{name}: materials not found', show_popup=True)

        # store the original surface node that is connected to material output node's surface socket 
        # in a custom property originally_connected_to_output_surface=True
        for mat in obj.data.materials:
            output_node = Utils.find_node(mat.node_tree.nodes, SearchNodeData(type=NodeTypes.OUTPUT_MATERIAL))
            if output_node and output_node.inputs["Surface"].links:
                original_node = output_node.inputs["Surface"].links[0].from_node
                original_node["originally_connected_to_output_surface"] = True
                self.report_message({'INFO'}, f'{name}: original surface output node stored in custom property')

        node_a_data = SearchNodeData(
            name=NODE_BAKE_IMAGE_NAME,
        )
        node_b_data = SearchNodeData(
            type=NodeTypes.OUTPUT_MATERIAL,

            create_if_not_found=True,
            create_type=NodeTypes.OUTPUT_MATERIAL_CREATE,
        )
        success = Utils.connect_nodes(obj, node_a_data, node_b_data,
                                      self.report_message)  # connect nodes for all materials
        if not success:
            return self.report_message({'WARNING'}, f'{name}: failed to connect nodes', show_popup=True)

        self.report_message({'INFO'}, f'{name}: switched to baked shading')


class Utils:  # class here is used just to group static methods
    @staticmethod
    def is_valid_mesh(obj: bpy.types.Object) -> bool:
        """
        Checks if the given object is a valid mesh object.
        :param obj
        :return:
        """
        return obj and obj.type == 'MESH'

    @staticmethod
    def select_object(obj: bpy.types.Object):
        """
        Selects the given object and sets it as active.
        :param obj:
        :return:
        """

        bpy.ops.object.select_all(action='DESELECT')  # deselect all objects
        obj.select_set(True)  # select the object
        bpy.context.view_layer.objects.active = obj  # set the object as active

    @staticmethod
    def find_node(nodes: bpy.types.Nodes, criteria: SearchNodeData) -> Optional[bpy.types.Node]:
        """
        Finds a node in the given list of nodes based on the given criteria.
        :param nodes: list of nodes to search in
        :param criteria: criteria to search for (SearchNodeData)
        :return: found node or None
        """

        if not nodes:
            return None

        for node in nodes:
            if any((
                    criteria.type and node.type == criteria.type,
                    criteria.name and node.name == criteria.name,
                    criteria.custom_property and node.get(criteria.custom_property) == criteria.custom_property_value,
            )):
                return node

        # it happens if the node is not found

        # if custom property is set but not found and fallback type is set - search for the node with the type
        if criteria.custom_property and criteria.custom_property_not_found_fallback_type:
            return Utils.find_node(nodes, SearchNodeData(type=criteria.custom_property_not_found_fallback_type))

        # if create_if_not_found is set - create a new node of the specified type and name
        # data for creation is validated in __post_init__
        if criteria.create_if_not_found:
            node = nodes.new(type=criteria.create_type)
            if criteria.create_name:
                node.name = criteria.create_name
            return node

        return None

    @staticmethod
    def connect_nodes(obj: bpy.types.Object, node_a_data: SearchNodeData, node_b_data: SearchNodeData,
                      report_message: callable) -> bool:
        """
        Connects two nodes for all materials in the material node tree of the given object.
        :param obj: the object to connect nodes for
        :param node_a_data: data to search/apply for the first node
        :param node_b_data: data to search/apply for the second node
        :param report_message: function to report messages
        :return: success
        """

        # search data
        for mat in obj.data.materials:
            mat.use_nodes = True  # enable nodes
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            node_a = Utils.find_node(nodes, node_a_data)
            node_b = Utils.find_node(nodes, node_b_data)

            if not node_a:
                report_message({'WARNING'}, f'{obj.name}: node_a not found', show_popup=True)
                return False
            if not node_b:
                report_message({'WARNING'}, f'{obj.name}: node_b not found', show_popup=True)
                return False

            # link node_a output to node_b input
            links.new(node_a.outputs[node_a_data.socket_index], node_b.inputs[node_b_data.socket_index])

        return True


def register():
    # operators
    bpy.utils.register_class(MessageBoxOperator)
    bpy.utils.register_class(BakeAllObjectsOperator)
    bpy.utils.register_class(SwitchToRealShadingOperator)
    bpy.utils.register_class(SwitchToBakedShadingOperator)

    # properties (settings)
    bpy.utils.register_class(BakeSettings)

    # ui
    bpy.utils.register_class(BakePanel)
    bpy.types.Scene.bake_settings = bpy.props.PointerProperty(type=BakeSettings)


def unregister():
    # operators
    bpy.utils.unregister_class(MessageBoxOperator)
    bpy.utils.unregister_class(BakeAllObjectsOperator)
    bpy.utils.unregister_class(SwitchToRealShadingOperator)
    bpy.utils.unregister_class(SwitchToBakedShadingOperator)

    # properties (settings)
    bpy.utils.unregister_class(BakeSettings)

    # ui
    bpy.utils.unregister_class(BakePanel)
    del bpy.types.Scene.bake_settings


if __name__ == '__main__':
    try:
        unregister()
    except:
        pass

    register()
