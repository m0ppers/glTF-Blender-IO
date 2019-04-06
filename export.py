import bpy
result = bpy.ops.export_scene.gltf(filepath="/home/mop/tmp/test", export_format="GLTF_EMBEDDED", export_selected=False)
print(result)