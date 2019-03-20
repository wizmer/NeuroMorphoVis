####################################################################################################
# Copyright (c) 2016 - 2018, EPFL / Blue Brain Project
#               Marwan Abdellah <marwan.abdellah@epfl.ch>
#
# This file is part of NeuroMorphoVis <https://github.com/BlueBrain/NeuroMorphoVis>
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, version 3 of the License.
#
# This Blender-based tool is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program.
# If not, see <http://www.gnu.org/licenses/>.
####################################################################################################

# System imports
import random, copy, sys

# Blender imports
import bpy
from mathutils import Vector

# Internal modules
import nmv
import nmv.builders
import nmv.bmeshi
import nmv.consts
import nmv.enums
import nmv.geometry
import nmv.mesh
import nmv.shading
import nmv.skeleton
import nmv.scene
import nmv.utilities


####################################################################################################
# @ExtrusionBuilder
####################################################################################################
class SkinningBuilder:
    """Mesh builder that creates accurate and nice meshes using skinning. The reconstructed meshes
    are not guaranteed to be watertight, but they look very nice if you need to use transparency."""

    ################################################################################################
    # @__init__
    ################################################################################################
    def __init__(self,
                 morphology,
                 options):
        """Constructor

        :param morphology:
            A given morphology skeleton to create the mesh for.
        :param options:
            Loaded options from NeuroMorphoVis.
        """

        # Morphology
        self.morphology = morphology

        # Loaded options from NeuroMorphoVis
        self.options = options

        # A list of the colors/materials of the soma
        self.soma_materials = None

        # A list of the colors/materials of the axon
        self.axon_materials = None

        # A list of the colors/materials of the basal dendrites
        self.basal_dendrites_materials = None

        # A list of the colors/materials of the apical dendrite
        self.apical_dendrites_materials = None

        # A list of the colors/materials of the spines
        self.spines_materials = None

        # A reference to the reconstructed soma mesh
        self.soma_mesh = None

        # A reference to the reconstructed spines mesh
        self.spines_mesh = None

    ################################################################################################
    # @verify_morphology_skeleton
    ################################################################################################
    def verify_morphology_skeleton(self):
        """Verifies and repairs the morphology if the contain any artifacts that would potentially
        affect the reconstruction quality of the mesh.

        NOTE: The filters or operations performed in this builder are only specific to it. Other
        builders might apply a different set of filters.
        """

        # Remove the internal samples, or the samples that intersect the soma at the first
        # section and each arbor
        nmv.skeleton.ops.apply_operation_to_morphology(
            *[self.morphology, nmv.skeleton.ops.remove_samples_inside_soma])

        # The arbors can be selected to be reconstructed with sharp edges or smooth ones. For the
        # sharp edges, we do NOT need to re-sample the morphology skeleton. However, if the smooth
        # edges option is selected, the arbors must be re-sampled to avoid any meshing artifacts
        # after applying the vertex smoothing filter. The re-sampling filter for the moment
        # re-samples the morphology sections at 2.5 microns, however this can be improved later
        # by adding an algorithm that re-samples the section based on its radii.
        if self.options.mesh.edges == nmv.enums.Meshing.Edges.SMOOTH:

            # Apply the re-sampling filter on the whole morphology skeleton
            nmv.skeleton.ops.apply_operation_to_morphology(
                *[self.morphology, nmv.skeleton.ops.resample_sections])

        # Verify the connectivity of the arbors to the soma to filter the disconnected arbors,
        # for example, an axon that is emanating from a dendrite or two intersecting dendrites
        nmv.skeleton.ops.update_arbors_connection_to_soma(self.morphology)

        # Label the primary and secondary sections based on radii, skinning is agnostic
        nmv.skeleton.ops.apply_operation_to_morphology(
            *[self.morphology,
              nmv.skeleton.ops.label_primary_and_secondary_sections_based_on_angles])

    ################################################################################################
    # @update_section_samples_radii
    ################################################################################################
    @staticmethod
    def update_section_samples_radii(arbor_mesh,
                                     section):
        """Update the radii of the samples along a given section.

        :param arbor_mesh:
            The mesh of the arbor.
        :param section:
            A given section to update the radii of its samples.
        """

        # Make sure to include the first sample of the root section
        if section.is_root():
            starting_index = 0
        else:
            starting_index = 1

        # Sample by sample along the section
        for i in range(starting_index, len(section.samples)):

            # Get the sample radius
            radius = section.samples[i].radius

            # Get a reference to the vertex
            vertex = arbor_mesh.data.skin_vertices[0].data[section.samples[i].arbor_idx]

            # Update the radius of the vertex
            vertex.radius = radius, radius

    ################################################################################################
    # @update_arbor_samples_radii
    ################################################################################################
    def update_arbor_samples_radii(self,
                                   arbor_mesh,
                                   root,
                                   max_branching_order):
        """Updates the radii of the samples of the entire arbor to match reality from the
        temporary ones that were given before.

        :param arbor_mesh:
            The mesh of the arbor that will be updated.
        :param root:
            The root section of the arbor.
        :param max_branching_order:
            The maximum branching order set by the user to terminate the recursive call.
        """

        # Do not proceed if the branching order limit is hit
        if root.branching_order > max_branching_order:
            return

        # Set the radius of a given section
        self.update_section_samples_radii(arbor_mesh, root)

        # Update the radii of the samples of the children recursively
        for child in root.children:
            self.update_arbor_samples_radii(arbor_mesh, child, max_branching_order)

    ################################################################################################
    # @extrude_section
    ################################################################################################
    @staticmethod
    def extrude_section(arbor_bmesh_object,
                        section):
        """Extrudes the section along its samples starting from the first one to the last one.

        Note that the mesh to be extruded is already selected and there is no need to pass it.

        :param arbor_bmesh_object:
            The bmesh object of the given arbor.
        :param section:
            A given section to extrude a mesh around it.
        """

        # Extrude segment by segment
        for i in range(len(section.samples) - 1):

            nmv.bmeshi.ops.extrude_vertex_towards_point(
                arbor_bmesh_object, section.samples[i].arbor_idx, section.samples[i + 1].point)

    ################################################################################################
    # @create_root_point_mesh
    ################################################################################################
    def extrude_arbor(self,
                      arbor_bmesh_object,
                      root,
                      max_branching_order):
        """Extrude the given arbor section by section recursively.

        :param root:
            The root of a given section.
        :param max_branching_order:
            The maximum branching order set by the user to terminate the recursive call.
        """

        # Do not proceed if the branching order limit is hit
        if root.branching_order > max_branching_order:
            return

        # Extrude the section
        self.extrude_section(arbor_bmesh_object, root)

        # Extrude the children sections recursively
        for child in root.children:
            self.extrude_arbor(arbor_bmesh_object, child, max_branching_order)


    def extrude_auxiliary_section_from_soma_center_to_arbor(self,
                                                            arbor,
                                                            arbor_bmesh_object,
                                                            number_samples):

        # Soma origin
        point_0 = Vector((0.0, 0.0, 0.0))

        # Initial sample of the arbor
        point_1 = arbor.samples[0].point

        # Compute the distance between the two points
        distance = (point_1 - point_0).length

        # Direction
        direction = (point_1 - point_0).normalized()

        # Step
        step = distance / number_samples

        for i in range(1, number_samples - 1):

            extrusion_point = point_0 + direction * step * i

            # Extrude to the first sample along the arbor
            nmv.bmeshi.ops.extrude_vertex_towards_point(arbor_bmesh_object, i - 1, extrusion_point)

    ################################################################################################
    # @create_arbor_mesh
    ################################################################################################
    def create_arbor_mesh(self,
                          arbor,
                          max_branching_order,
                          arbor_name,
                          arbor_material):
        """Creates a mesh of the given arbor recursively.

        :param arbor:
            A given arbor.
        :param max_branching_order:
            The maximum branching order of the arbor.
        :param arbor_name:
            The name of the arbor.
        :param arbor_material:
            The material or the arbor.
        :return:
            A reference to the created mesh object.
        """

        number_samples = 5

        # Initially, this index is set to ONE and incremented later, sample zero is reserved to
        # the auxiliary sample that is added at the soma
        samples_global_arbor_index = [2]
        nmv.builders.update_samples_indices_per_arbor(
            arbor, samples_global_arbor_index, max_branching_order)

        # Create the initial vertex of the arbor skeleton
        arbor_bmesh_object = nmv.bmeshi.create_vertex()

        #self.extrude_auxiliary_section_from_soma_center_to_arbor(arbor, arbor_bmesh_object, number_samples)

        # Extrude to the first sample along the arbor
        # nmv.bmeshi.ops.extrude_vertex_towards_point(arbor_bmesh_object, 0, Vector((0, 0, 0)))


        direction = arbor.samples[0].point.normalized()
        p0 = arbor.samples[0].point - 0.01 * direction

        # Extrude to the first sample along the arbor
        nmv.bmeshi.ops.extrude_vertex_towards_point(arbor_bmesh_object, 0, p0)
        nmv.bmeshi.ops.extrude_vertex_towards_point(arbor_bmesh_object, 1, arbor.samples[0].point)

        # Extrude arbor mesh using the skinning method using a temporary radius with a bmesh
        self.extrude_arbor(arbor_bmesh_object, arbor, max_branching_order)

        # Convert the bmesh to a mesh object
        arbor_mesh = nmv.bmeshi.convert_bmesh_to_mesh(arbor_bmesh_object, arbor_name)

        # Apply a skin modifier create the membrane of the skeleton
        arbor_mesh.modifiers.new(name="Skin", type='SKIN')

        # Activate the arbor mesh
        nmv.scene.set_active_object(arbor_mesh)

        # Get a reference to the vertex
        vertex = arbor_mesh.data.skin_vertices[0].data[0]

        # Update the radius of the vertex
        vertex.radius = arbor.samples[0].radius, arbor.samples[0].radius

        # Get a reference to the vertex
        vertex = arbor_mesh.data.skin_vertices[0].data[1]

        # Update the radius of the vertex
        vertex.radius = arbor.samples[0].radius, arbor.samples[0].radius

        # Update the radii of the arbor using the fast method before applying the skinning modifier
        self.update_arbor_samples_radii(
            arbor_mesh=arbor_mesh, root=arbor, max_branching_order=max_branching_order)

        # Apply the modifier
        bpy.ops.object.modifier_apply(apply_as='DATA', modifier="Skin")

        # Smooth the mesh object
        nmv.mesh.smooth_object(mesh_object=arbor_mesh, level=2)

        # Further smoothing, only with shading
        nmv.mesh.shade_smooth_object(arbor_mesh)

        # Assign the material to the reconstructed arbor mesh
        nmv.shading.set_material_to_object(arbor_mesh, arbor_material)

        # Return a reference to the arbor mesh
        return arbor_mesh

    ################################################################################################
    # @build_arbors
    ################################################################################################
    def build_arbors(self):
        """Builds the arbors of the neuron as tubes and AT THE END converts them into meshes.
        If you convert them during the building, the scene is getting crowded and the process is
        getting exponentially slower.
        """

        # Header
        nmv.logger.header('Building Arbors')

        # Draw the apical dendrite, if exists
        if not self.options.morphology.ignore_apical_dendrite:
            nmv.logger.info('Apical dendrite')

            # Create the apical dendrite mesh
            if self.morphology.apical_dendrite is not None:

                arbor_mesh = self.create_arbor_mesh(
                    arbor=self.morphology.apical_dendrite,
                    max_branching_order=self.options.morphology.apical_dendrite_branch_order,
                    arbor_name=nmv.consts.Arbors.APICAL_DENDRITES_PREFIX,
                    arbor_material= self.apical_dendrites_materials[0])

                # Add a reference to the mesh object
                self.morphology.apical_dendrite.mesh = arbor_mesh

        # Draw the basal dendrites
        if not self.options.morphology.ignore_basal_dendrites:

            # Do it dendrite by dendrite
            for i, basal_dendrite in enumerate(self.morphology.dendrites):
                # Create the basal dendrite meshes
                nmv.logger.info('Dendrite [%d]' % i)
                arbor_mesh = self.create_arbor_mesh(
                    arbor=basal_dendrite,
                    max_branching_order=self.options.morphology.basal_dendrites_branch_order,
                    arbor_name='%s_%d' % (nmv.consts.Arbors.BASAL_DENDRITES_PREFIX, i),
                    arbor_material=self.basal_dendrites_materials[0])

                # Add a reference to the mesh object
                self.morphology.dendrites[i].mesh = arbor_mesh

        # Draw the axon as a set connected sections
        if not self.options.morphology.ignore_axon:

            # Ensure tha existence of basal dendrites
            if self.morphology.axon is not None:
                nmv.logger.info('Axon')

                # Create the axon mesh
                arbor_mesh = self.create_arbor_mesh(
                    arbor=self.morphology.axon,
                    max_branching_order=self.options.morphology.axon_branch_order,
                    arbor_name=nmv.consts.Arbors.AXON_PREFIX,
                    arbor_material=self.axon_materials[0])

                # Add a reference to the mesh object
                self.morphology.axon.mesh = arbor_mesh


    ################################################################################################
    # @reconstruct_mesh
    ################################################################################################
    def reconstruct_mesh(self):
        """Reconstructs the neuronal mesh using the skinning modifiers in Blender.
        """

        # NOTE: Before drawing the skeleton, create the materials once and for all to improve the
        # performance since this is way better than creating a new material per section or segment
        nmv.builders.common.create_skeleton_materials(builder=self)

        # Verify and repair the morphology, if required
        self.verify_morphology_skeleton()

        # Apply skeleton-based operation, if required, to slightly modify the skeleton
        nmv.builders.common.modify_morphology_skeleton()

        # Build the soma, with the default parameters
        nmv.builders.common.reconstruct_soma_mesh(builder=self)

        # Build the arbors
        self.build_arbors()

        # if self.options.mesh.surface == nmv.enums.Meshing.Surface.ROUGH:
        #    nmv.builders.common.add_surface_noise_to_arbor(builder=self)

        nmv.logger.header('Done!')
