# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright 2010 OpenStack Foundation
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Management class for VM-related functions (spawn, reboot, etc).
"""

import base64
import functools
import itertools
import time
import zlib

from eventlet import greenthread
import netaddr
from oslo.config import cfg

from nova import block_device
from nova.compute import flavors
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
from nova.compute import vm_states
from nova import conductor
from nova import context as nova_context
from nova import exception
from nova.openstack.common import excutils
from nova.openstack.common.gettextutils import _
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import timeutils
from nova import utils
from nova.virt import configdrive
from nova.virt import driver as virt_driver
from nova.virt import firewall
from nova.virt.xenapi import agent as xapi_agent
from nova.virt.xenapi import pool_states
from nova.virt.xenapi import vm_utils
from nova.virt.xenapi import volume_utils
from nova.virt.xenapi import volumeops


LOG = logging.getLogger(__name__)

xenapi_vmops_opts = [
    cfg.IntOpt('xenapi_running_timeout',
               default=60,
               help='number of seconds to wait for instance '
                    'to go to running state'),
    cfg.StrOpt('xenapi_vif_driver',
               default='nova.virt.xenapi.vif.XenAPIBridgeDriver',
               help='The XenAPI VIF driver using XenServer Network APIs.'),
    cfg.StrOpt('xenapi_image_upload_handler',
                default='nova.virt.xenapi.imageupload.glance.GlanceStore',
                help='Object Store Driver used to handle image uploads.'),
    ]

CONF = cfg.CONF
CONF.register_opts(xenapi_vmops_opts)
CONF.import_opt('host', 'nova.netconf')
CONF.import_opt('vncserver_proxyclient_address', 'nova.vnc')

DEFAULT_FIREWALL_DRIVER = "%s.%s" % (
    firewall.__name__,
    firewall.IptablesFirewallDriver.__name__)

RESIZE_TOTAL_STEPS = 5

DEVICE_ROOT = '0'
DEVICE_RESCUE = '1'
DEVICE_SWAP = '2'
DEVICE_CONFIGDRIVE = '3'
# Note(johngarbutt) HVM guests only support four devices
# until the PV tools activate, when others before available
# As such, ephemeral disk only available once PV tools load
# Note(johngarbutt) When very large ephemeral storage is required,
# multiple disks may be added. In this case the device id below
# is the used for the first disk. The second disk will be given
# next device id, i.e. 5, and so on, until enough space is added.
DEVICE_EPHEMERAL = '4'
# Note(johngarbutt) Currently don't support ISO boot during rescue
# and we must have the ISO visible before the PV drivers start
DEVICE_CD = '1'


def cmp_version(a, b):
    """Compare two version strings (eg 0.0.1.10 > 0.0.1.9)."""
    a = a.split('.')
    b = b.split('.')

    # Compare each individual portion of both version strings
    for va, vb in zip(a, b):
        ret = int(va) - int(vb)
        if ret:
            return ret

    # Fallback to comparing length last
    return len(a) - len(b)


def make_step_decorator(context, instance, instance_update):
    """Factory to create a decorator that records instance progress as a series
    of discrete steps.

    Each time the decorator is invoked we bump the total-step-count, so after::

        @step
        def step1():
            ...

        @step
        def step2():
            ...

    we have a total-step-count of 2.

    Each time the step-function (not the step-decorator!) is invoked, we bump
    the current-step-count by 1, so after::

        step1()

    the current-step-count would be 1 giving a progress of ``1 / 2 *
    100`` or 50%.
    """
    step_info = dict(total=0, current=0)

    def bump_progress():
        step_info['current'] += 1
        progress = round(float(step_info['current']) /
                         step_info['total'] * 100)
        LOG.debug(_("Updating progress to %d"), progress, instance=instance)
        instance_update(context, instance['uuid'], {'progress': progress})

    def step_decorator(f):
        step_info['total'] += 1

        @functools.wraps(f)
        def inner(*args, **kwargs):
            rv = f(*args, **kwargs)
            bump_progress()
            return rv

        return inner

    return step_decorator


class VMOps(object):
    """
    Management class for VM-related tasks
    """
    def __init__(self, session, virtapi):
        self.conductor_api = conductor.API()
        self._session = session
        self._virtapi = virtapi
        self._volumeops = volumeops.VolumeOps(self._session)
        self.firewall_driver = firewall.load_driver(
            DEFAULT_FIREWALL_DRIVER,
            self._virtapi,
            xenapi_session=self._session)
        vif_impl = importutils.import_class(CONF.xenapi_vif_driver)
        self.vif_driver = vif_impl(xenapi_session=self._session)
        self.default_root_dev = '/dev/sda'

        LOG.debug(_("Importing image upload handler: %s"),
                  CONF.xenapi_image_upload_handler)
        self.image_upload_handler = importutils.import_object(
                                CONF.xenapi_image_upload_handler)

    def agent_enabled(self, instance):
        if CONF.xenapi_disable_agent:
            return False

        return xapi_agent.should_use_agent(instance)

    def _get_agent(self, instance, vm_ref):
        if self.agent_enabled(instance):
            return xapi_agent.XenAPIBasedAgent(self._session, self._virtapi,
                                               instance, vm_ref)
        raise exception.NovaException(_("Error: Agent is disabled"))

    def list_instances(self):
        """List VM instances."""
        # TODO(justinsb): Should we just always use the details method?
        #  Seems to be the same number of API calls..
        name_labels = []
        for vm_ref, vm_rec in vm_utils.list_vms(self._session):
            name_labels.append(vm_rec["name_label"])

        return name_labels

    def list_instance_uuids(self):
        """Get the list of nova instance uuids for VMs found on the
        hypervisor.
        """
        nova_uuids = []
        for vm_ref, vm_rec in vm_utils.list_vms(self._session):
            other_config = vm_rec['other_config']
            nova_uuid = other_config.get('nova_uuid')
            if nova_uuid:
                nova_uuids.append(nova_uuid)
        return nova_uuids

    def confirm_migration(self, migration, instance, network_info):
        self._destroy_orig_vm(instance, network_info)

    def _destroy_orig_vm(self, instance, network_info):
        name_label = self._get_orig_vm_name_label(instance)
        vm_ref = vm_utils.lookup(self._session, name_label)
        return self._destroy(instance, vm_ref, network_info=network_info)

    def _attach_mapped_block_devices(self, instance, block_device_info):
        # We are attaching these volumes before start (no hotplugging)
        # because some guests (windows) don't load PV drivers quickly
        block_device_mapping = virt_driver.block_device_info_get_mapping(
                block_device_info)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mount_device = vol['mount_device'].rpartition("/")[2]
            self._volumeops.attach_volume(connection_info,
                                          instance['name'],
                                          mount_device,
                                          hotplug=False)

    def finish_revert_migration(self, instance, block_device_info=None,
                                power_on=True):
        self._restore_orig_vm_and_cleanup_orphan(instance, block_device_info,
                                                 power_on)

    def _restore_orig_vm_and_cleanup_orphan(self, instance,
                                            block_device_info, power_on=True):
        # NOTE(sirp): the original vm was suffixed with '-orig'; find it using
        # the old suffix, remove the suffix, then power it back on.
        name_label = self._get_orig_vm_name_label(instance)
        vm_ref = vm_utils.lookup(self._session, name_label)

        # NOTE(danms): if we're reverting migration in the failure case,
        # make sure we don't have a conflicting vm still running here,
        # as might be the case in a failed migrate-to-same-host situation
        new_ref = vm_utils.lookup(self._session, instance['name'])
        if vm_ref is not None:
            if new_ref is not None:
                self._destroy(instance, new_ref)
            # Remove the '-orig' suffix (which was added in case the
            # resized VM ends up on the source host, common during
            # testing)
            name_label = instance['name']
            vm_utils.set_vm_name_label(self._session, vm_ref, name_label)
            self._attach_mapped_block_devices(instance, block_device_info)
        elif new_ref is not None:
            # We crashed before the -orig backup was made
            vm_ref = new_ref

        if power_on:
            self._start(instance, vm_ref)

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        root_vdi = vm_utils.move_disks(self._session, instance, disk_info)

        if resize_instance:
            self._resize_instance(instance, root_vdi)

        # Check if kernel and ramdisk are external
        kernel_file = None
        ramdisk_file = None

        name_label = instance['name']
        if instance['kernel_id']:
            vdis = vm_utils.create_kernel_image(context, self._session,
                        instance, name_label, instance['kernel_id'],
                        vm_utils.ImageType.KERNEL)
            kernel_file = vdis['kernel'].get('file')
        if instance['ramdisk_id']:
            vdis = vm_utils.create_kernel_image(context, self._session,
                        instance, name_label, instance['ramdisk_id'],
                        vm_utils.ImageType.RAMDISK)
            ramdisk_file = vdis['ramdisk'].get('file')

        disk_image_type = vm_utils.determine_disk_image_type(image_meta)
        vm_ref = self._create_vm(context, instance, instance['name'],
                                 {'root': root_vdi},
                                 disk_image_type, network_info, kernel_file,
                                 ramdisk_file)

        self._attach_mapped_block_devices(instance, block_device_info)

        # 5. Start VM
        if power_on:
            self._start(instance, vm_ref=vm_ref)
        self._update_instance_progress(context, instance,
                                       step=5,
                                       total_steps=RESIZE_TOTAL_STEPS)

    def _start(self, instance, vm_ref=None, bad_volumes_callback=None):
        """Power on a VM instance."""
        vm_ref = vm_ref or self._get_vm_opaque_ref(instance)
        LOG.debug(_("Starting instance"), instance=instance)

        # Attached volumes that have become non-responsive will prevent a VM
        # from starting, so scan for these before attempting to start
        #
        # In order to make sure this detach is consistent (virt, BDM, cinder),
        # we only detach in the virt-layer if a callback is provided.
        if bad_volumes_callback:
            bad_devices = self._volumeops.find_bad_volumes(vm_ref)
            for device_name in bad_devices:
                self._volumeops.detach_volume(
                        None, instance['name'], device_name)

        self._session.call_xenapi('VM.start_on', vm_ref,
                                  self._session.get_xenapi_host(),
                                  False, False)

        # Allow higher-layers a chance to detach bad-volumes as well (in order
        # to cleanup BDM entries and detach in Cinder)
        if bad_volumes_callback and bad_devices:
            bad_volumes_callback(bad_devices)

    def _create_disks(self, context, instance, name_label, disk_image_type,
                      image_meta, block_device_info=None):
        vdis = vm_utils.get_vdis_for_instance(context, self._session,
                                          instance, name_label,
                                          image_meta.get('id'),
                                          disk_image_type,
                                          block_device_info=block_device_info)
        # Just get the VDI ref once
        for vdi in vdis.itervalues():
            vdi['ref'] = self._session.call_xenapi('VDI.get_by_uuid',
                                                   vdi['uuid'])

        root_vdi = vdis.get('root')
        if root_vdi:
            self._resize_instance(instance, root_vdi)

        return vdis

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None,
              name_label=None, rescue=False):
        if name_label is None:
            name_label = instance['name']

        step = make_step_decorator(context, instance,
                                   self._virtapi.instance_update)

        @step
        def determine_disk_image_type_step(undo_mgr):
            return vm_utils.determine_disk_image_type(image_meta)

        @step
        def create_disks_step(undo_mgr, disk_image_type, image_meta):
            vdis = self._create_disks(context, instance, name_label,
                                      disk_image_type, image_meta,
                                      block_device_info=block_device_info)

            def undo_create_disks():
                vdi_refs = [vdi['ref'] for vdi in vdis.values()
                        if not vdi.get('osvol')]
                vm_utils.safe_destroy_vdis(self._session, vdi_refs)

            undo_mgr.undo_with(undo_create_disks)
            return vdis

        @step
        def create_kernel_ramdisk_step(undo_mgr):
            kernel_file = None
            ramdisk_file = None

            if instance['kernel_id']:
                vdis = vm_utils.create_kernel_image(context, self._session,
                        instance, name_label, instance['kernel_id'],
                        vm_utils.ImageType.KERNEL)
                kernel_file = vdis['kernel'].get('file')

            if instance['ramdisk_id']:
                vdis = vm_utils.create_kernel_image(context, self._session,
                        instance, name_label, instance['ramdisk_id'],
                        vm_utils.ImageType.RAMDISK)
                ramdisk_file = vdis['ramdisk'].get('file')

            def undo_create_kernel_ramdisk():
                if kernel_file or ramdisk_file:
                    LOG.debug(_("Removing kernel/ramdisk files from dom0"),
                              instance=instance)
                    vm_utils.destroy_kernel_ramdisk(
                            self._session, kernel_file, ramdisk_file)

            undo_mgr.undo_with(undo_create_kernel_ramdisk)
            return kernel_file, ramdisk_file

        @step
        def create_vm_record_step(undo_mgr, vdis, disk_image_type,
                kernel_file, ramdisk_file):
            vm_ref = self._create_vm_record(context, instance, name_label,
                    vdis, disk_image_type, kernel_file, ramdisk_file)

            def undo_create_vm():
                self._destroy(instance, vm_ref, network_info=network_info)

            undo_mgr.undo_with(undo_create_vm)
            return vm_ref

        @step
        def attach_disks_step(undo_mgr, vm_ref, vdis, disk_image_type):
            self._attach_disks(instance, vm_ref, name_label, vdis,
                               disk_image_type, admin_password,
                               injected_files)

        if rescue:
            # NOTE(johannes): Attach root disk to rescue VM now, before
            # booting the VM, since we can't hotplug block devices
            # on non-PV guests
            @step
            def attach_root_disk_step(undo_mgr, vm_ref):
                orig_vm_ref = vm_utils.lookup(self._session, instance['name'])
                vdi_ref = self._find_root_vdi_ref(orig_vm_ref)

                vm_utils.create_vbd(self._session, vm_ref, vdi_ref,
                                    DEVICE_RESCUE, bootable=False)

        @step
        def setup_network_step(undo_mgr, vm_ref, vdis):
            self._setup_vm_networking(instance, vm_ref, vdis, network_info,
                    rescue)

        @step
        def inject_instance_data_step(undo_mgr, vm_ref):
            self.inject_instance_metadata(instance, vm_ref)
            self.inject_auto_disk_config(instance, vm_ref)

        @step
        def prepare_security_group_filters_step(undo_mgr):
            try:
                self.firewall_driver.setup_basic_filtering(
                        instance, network_info)
            except NotImplementedError:
                # NOTE(salvatore-orlando): setup_basic_filtering might be
                # empty or not implemented at all, as basic filter could
                # be implemented with VIF rules created by xapi plugin
                pass

            self.firewall_driver.prepare_instance_filter(instance,
                                                         network_info)

        @step
        def boot_instance_step(undo_mgr, vm_ref):
            self._boot_new_instance(instance, vm_ref, injected_files,
                                    admin_password)

        @step
        def apply_security_group_filters_step(undo_mgr):
            self.firewall_driver.apply_instance_filter(instance, network_info)

        @step
        def bdev_set_default_root(undo_mgr):
            if block_device_info:
                LOG.debug(_("Block device information present: %s")
                          % block_device_info, instance=instance)
            if block_device_info and not block_device_info['root_device_name']:
                block_device_info['root_device_name'] = self.default_root_dev

        undo_mgr = utils.UndoManager()
        try:
            # NOTE(sirp): The create_disks() step will potentially take a
            # *very* long time to complete since it has to fetch the image
            # over the network and images can be several gigs in size. To
            # avoid progress remaining at 0% for too long, make sure the
            # first step is something that completes rather quickly.
            bdev_set_default_root(undo_mgr)
            disk_image_type = determine_disk_image_type_step(undo_mgr)

            vdis = create_disks_step(undo_mgr, disk_image_type, image_meta)
            kernel_file, ramdisk_file = create_kernel_ramdisk_step(undo_mgr)
            vm_ref = create_vm_record_step(undo_mgr, vdis, disk_image_type,
                    kernel_file, ramdisk_file)
            attach_disks_step(undo_mgr, vm_ref, vdis, disk_image_type)
            setup_network_step(undo_mgr, vm_ref, vdis)
            inject_instance_data_step(undo_mgr, vm_ref)
            prepare_security_group_filters_step(undo_mgr)

            if rescue:
                attach_root_disk_step(undo_mgr, vm_ref)

            boot_instance_step(undo_mgr, vm_ref)

            apply_security_group_filters_step(undo_mgr)
        except Exception:
            msg = _("Failed to spawn, rolling back")
            undo_mgr.rollback_and_reraise(msg=msg, instance=instance)

    def _create_vm(self, context, instance, name_label, vdis,
            disk_image_type, network_info, kernel_file=None,
            ramdisk_file=None, rescue=False):
        """Create VM instance."""
        vm_ref = self._create_vm_record(context, instance, name_label,
                vdis, disk_image_type, kernel_file, ramdisk_file)
        self._attach_disks(instance, vm_ref, name_label, vdis,
                disk_image_type)
        self._setup_vm_networking(instance, vm_ref, vdis, network_info,
                rescue)
        self.inject_instance_metadata(instance, vm_ref)

        return vm_ref

    def _setup_vm_networking(self, instance, vm_ref, vdis, network_info,
            rescue):
        # Alter the image before VM start for network injection.
        if CONF.flat_injected:
            vm_utils.preconfigure_instance(self._session, instance,
                                           vdis['root']['ref'], network_info)

        self._create_vifs(vm_ref, instance, network_info)
        self.inject_network_info(instance, network_info, vm_ref)

        hostname = instance['hostname']
        if rescue:
            hostname = 'RESCUE-%s' % hostname
        self.inject_hostname(instance, vm_ref, hostname)

    def _create_vm_record(self, context, instance, name_label, vdis,
            disk_image_type, kernel_file, ramdisk_file):
        """Create the VM record in Xen, making sure that we do not create
        a duplicate name-label.  Also do a rough sanity check on memory
        to try to short-circuit a potential failure later.  (The memory
        check only accounts for running VMs, so it can miss other builds
        that are in progress.)
        """
        vm_ref = vm_utils.lookup(self._session, name_label)
        if vm_ref is not None:
            raise exception.InstanceExists(name=name_label)

        # Ensure enough free memory is available
        if not vm_utils.ensure_free_mem(self._session, instance):
            raise exception.InsufficientFreeMemory(uuid=instance['uuid'])

        mode = self._determine_vm_mode(instance, vdis, disk_image_type)
        if instance['vm_mode'] != mode:
            # Update database with normalized (or determined) value
            self._virtapi.instance_update(context,
                                          instance['uuid'], {'vm_mode': mode})

        use_pv_kernel = (mode == vm_mode.XEN)
        vm_ref = vm_utils.create_vm(self._session, instance, name_label,
                                    kernel_file, ramdisk_file, use_pv_kernel)
        return vm_ref

    def _determine_vm_mode(self, instance, vdis, disk_image_type):
        current_mode = vm_mode.get_from_instance(instance)
        if current_mode == vm_mode.XEN or current_mode == vm_mode.HVM:
            return current_mode

        is_pv = False
        if 'root' in vdis:
            os_type = instance['os_type']
            vdi_ref = vdis['root']['ref']
            is_pv = vm_utils.determine_is_pv(self._session, vdi_ref,
                                             disk_image_type, os_type)
        if is_pv:
            return vm_mode.XEN
        else:
            return vm_mode.HVM

    def _attach_disks(self, instance, vm_ref, name_label, vdis,
                      disk_image_type, admin_password=None, files=None):
        ctx = nova_context.get_admin_context()
        instance_type = flavors.extract_flavor(instance)

        # Attach (required) root disk
        if disk_image_type == vm_utils.ImageType.DISK_ISO:
            # DISK_ISO needs two VBDs: the ISO disk and a blank RW disk
            root_disk_size = instance_type['root_gb']
            if root_disk_size > 0:
                vm_utils.generate_iso_blank_root_disk(self._session, instance,
                    vm_ref, DEVICE_ROOT, name_label, root_disk_size)

            cd_vdi = vdis.pop('iso')
            vm_utils.attach_cd(self._session, vm_ref, cd_vdi['ref'],
                               DEVICE_CD)
        else:
            root_vdi = vdis['root']

            if instance['auto_disk_config']:
                LOG.debug(_("Auto configuring disk, attempting to "
                            "resize partition..."), instance=instance)
                vm_utils.try_auto_configure_disk(self._session,
                                                 root_vdi['ref'],
                                                 instance_type['root_gb'])

            vm_utils.create_vbd(self._session, vm_ref, root_vdi['ref'],
                                DEVICE_ROOT, bootable=True,
                                osvol=root_vdi.get('osvol'))

        # Attach (optional) additional block-devices
        for type_, vdi_info in vdis.items():
            # Additional block-devices for boot use their device-name as the
            # type.
            if not type_.startswith('/dev'):
                continue

            # Convert device name to userdevice number, e.g. /dev/xvdb -> 1
            userdevice = ord(block_device.strip_prefix(type_)) - ord('a')
            vm_utils.create_vbd(self._session, vm_ref, vdi_info['ref'],
                                userdevice, bootable=False,
                                osvol=vdi_info.get('osvol'))

        # Attach (optional) swap disk
        swap_mb = instance_type['swap']
        if swap_mb:
            vm_utils.generate_swap(self._session, instance, vm_ref,
                                   DEVICE_SWAP, name_label, swap_mb)

        # Attach (optional) ephemeral disk
        ephemeral_gb = instance_type['ephemeral_gb']
        if ephemeral_gb:
            vm_utils.generate_ephemeral(self._session, instance, vm_ref,
                                        DEVICE_EPHEMERAL, name_label,
                                        ephemeral_gb)

        # Attach (optional) configdrive v2 disk
        if configdrive.required_by(instance):
            vm_utils.generate_configdrive(self._session, instance, vm_ref,
                                          DEVICE_CONFIGDRIVE,
                                          admin_password=admin_password,
                                          files=files)

    def _boot_new_instance(self, instance, vm_ref, injected_files,
                           admin_password):
        """Boot a new instance and configure it."""
        LOG.debug(_('Starting VM'), instance=instance)
        self._start(instance, vm_ref)

        ctx = nova_context.get_admin_context()

        # Wait for boot to finish
        LOG.debug(_('Waiting for instance state to become running'),
                  instance=instance)
        expiration = time.time() + CONF.xenapi_running_timeout
        while time.time() < expiration:
            state = self.get_info(instance, vm_ref)['state']
            if state == power_state.RUNNING:
                break

            greenthread.sleep(0.5)

        if self.agent_enabled(instance):
            agent_build = self._virtapi.agent_build_get_by_triple(
                ctx, 'xen', instance['os_type'], instance['architecture'])
            if agent_build:
                LOG.info(_('Latest agent build for %(hypervisor)s/%(os)s'
                           '/%(architecture)s is %(version)s') % agent_build)
            else:
                LOG.info(_('No agent build found for %(hypervisor)s/%(os)s'
                           '/%(architecture)s') % {
                            'hypervisor': 'xen',
                            'os': instance['os_type'],
                            'architecture': instance['architecture']})

            # Update agent, if necessary
            # This also waits until the agent starts
            agent = self._get_agent(instance, vm_ref)
            version = agent.get_agent_version()
            if version:
                LOG.info(_('Instance agent version: %s'), version,
                         instance=instance)

            if (version and agent_build and
                    cmp_version(version, agent_build['version']) < 0):
                agent.agent_update(agent_build)

            # if the guest agent is not available, configure the
            # instance, but skip the admin password configuration
            no_agent = version is None

            # Inject ssh key.
            agent.inject_ssh_key()

            # Inject files, if necessary
            if injected_files:
                # Inject any files, if specified
                for path, contents in injected_files:
                    agent.inject_file(path, contents)

            # Set admin password, if necessary
            if admin_password and not no_agent:
                agent.set_admin_password(admin_password)

            # Reset network config
            agent.resetnetwork()

        self.remove_hostname(instance, vm_ref)

    def _get_vm_opaque_ref(self, instance, check_rescue=False):
        """Get xapi OpaqueRef from a db record.
        :param check_rescue: if True will return the 'name'-rescue vm if it
                             exists, instead of just 'name'
        """
        vm_ref = vm_utils.lookup(self._session, instance['name'], check_rescue)
        if vm_ref is None:
            raise exception.NotFound(_('Could not find VM with name %s') %
                                     instance['name'])
        return vm_ref

    def _acquire_bootlock(self, vm):
        """Prevent an instance from booting."""
        self._session.call_xenapi(
            "VM.set_blocked_operations",
            vm,
            {"start": ""})

    def _release_bootlock(self, vm):
        """Allow an instance to boot."""
        self._session.call_xenapi(
            "VM.remove_from_blocked_operations",
            vm,
            "start")

    def snapshot(self, context, instance, image_id, update_task_state):
        """Create snapshot from a running VM instance.

        :param context: request context
        :param instance: instance to be snapshotted
        :param image_id: id of image to upload to

        Steps involved in a XenServer snapshot:

        1. XAPI-Snapshot: Snapshotting the instance using XenAPI. This
           creates: Snapshot (Template) VM, Snapshot VBD, Snapshot VDI,
           Snapshot VHD

        2. Wait-for-coalesce: The Snapshot VDI and Instance VDI both point to
           a 'base-copy' VDI.  The base_copy is immutable and may be chained
           with other base_copies.  If chained, the base_copies
           coalesce together, so, we must wait for this coalescing to occur to
           get a stable representation of the data on disk.

        3. Push-to-data-store: Once coalesced, we call a plugin on the
           XenServer that will bundle the VHDs together and then push the
           bundle. Depending on the configured value of
           'xenapi_image_upload_handler', image data may be pushed to
           Glance or the specified data store.

        """
        vm_ref = self._get_vm_opaque_ref(instance)
        label = "%s-snapshot" % instance['name']

        with vm_utils.snapshot_attached_here(
                self._session, instance, vm_ref, label,
                update_task_state) as vdi_uuids:
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)
            self.image_upload_handler.upload_image(context,
                                                   self._session,
                                                   instance,
                                                   vdi_uuids,
                                                   image_id)

        LOG.debug(_("Finished snapshot and upload for VM"),
                  instance=instance)

    def _migrate_vhd(self, instance, vdi_uuid, dest, sr_path, seq_num):
        LOG.debug(_("Migrating VHD '%(vdi_uuid)s' with seq_num %(seq_num)d"),
                  {'vdi_uuid': vdi_uuid, 'seq_num': seq_num},
                  instance=instance)
        instance_uuid = instance['uuid']
        try:
            self._session.call_plugin_serialized('migration', 'transfer_vhd',
                    instance_uuid=instance_uuid, host=dest, vdi_uuid=vdi_uuid,
                    sr_path=sr_path, seq_num=seq_num)
        except self._session.XenAPI.Failure:
            msg = _("Failed to transfer vhd to new host")
            raise exception.MigrationError(reason=msg)

    def _get_orig_vm_name_label(self, instance):
        return instance['name'] + '-orig'

    def _update_instance_progress(self, context, instance, step, total_steps):
        """Update instance progress percent to reflect current step number
        """
        # FIXME(sirp): for now we're taking a KISS approach to instance
        # progress:
        # Divide the action's workflow into discrete steps and "bump" the
        # instance's progress field as each step is completed.
        #
        # For a first cut this should be fine, however, for large VM images,
        # the _create_disks step begins to dominate the equation. A
        # better approximation would use the percentage of the VM image that
        # has been streamed to the destination host.
        progress = round(float(step) / total_steps * 100)
        LOG.debug(_("Updating progress to %d"), progress,
                  instance=instance)
        self._virtapi.instance_update(context, instance['uuid'],
                                      {'progress': progress})

    def _resize_ensure_vm_is_shutdown(self, instance, vm_ref):
        if vm_utils.is_vm_shutdown(self._session, vm_ref):
            LOG.debug(_("VM was already shutdown."), instance=instance)
            return

        if not vm_utils.clean_shutdown_vm(self._session, instance, vm_ref):
            LOG.debug(_("Clean shutdown did not complete successfully, "
                        "trying hard shutdown."), instance=instance)
            if not vm_utils.hard_shutdown_vm(self._session, instance, vm_ref):
                raise exception.ResizeError(
                    reason=_("Unable to terminate instance."))

    def _migrate_disk_resizing_down(self, context, instance, dest,
                                    instance_type, vm_ref, sr_path):
        step = make_step_decorator(context, instance,
                                   self._virtapi.instance_update)

        @step
        def fake_step_to_match_resizing_up():
            pass

        @step
        def rename_and_power_off_vm(undo_mgr):
            self._resize_ensure_vm_is_shutdown(instance, vm_ref)
            self._apply_orig_vm_name_label(instance, vm_ref)

            def restore_orig_vm():
                # Do not need to restore block devices, not yet been removed
                self._restore_orig_vm_and_cleanup_orphan(instance, None)

            undo_mgr.undo_with(restore_orig_vm)

        @step
        def create_copy_vdi_and_resize(undo_mgr, old_vdi_ref):
            new_vdi_ref, new_vdi_uuid = vm_utils.resize_disk(self._session,
                instance, old_vdi_ref, instance_type)

            def cleanup_vdi_copy():
                vm_utils.destroy_vdi(self._session, new_vdi_ref)

            undo_mgr.undo_with(cleanup_vdi_copy)

            return new_vdi_ref, new_vdi_uuid

        @step
        def transfer_vhd_to_dest(new_vdi_ref, new_vdi_uuid):
            self._migrate_vhd(instance, new_vdi_uuid, dest, sr_path, 0)
            # Clean up VDI now that it's been copied
            vm_utils.destroy_vdi(self._session, new_vdi_ref)

        @step
        def fake_step_to_be_executed_by_finish_migration():
            pass

        undo_mgr = utils.UndoManager()
        try:
            fake_step_to_match_resizing_up()
            rename_and_power_off_vm(undo_mgr)
            old_vdi_ref, _ignore = vm_utils.get_vdi_for_vm_safely(
                self._session, vm_ref)
            new_vdi_ref, new_vdi_uuid = create_copy_vdi_and_resize(
                undo_mgr, old_vdi_ref)
            transfer_vhd_to_dest(new_vdi_ref, new_vdi_uuid)
        except Exception as error:
            LOG.exception(_("_migrate_disk_resizing_down failed. "
                            "Restoring orig vm due_to: %s."), error,
                          instance=instance)
            undo_mgr._rollback()
            raise exception.InstanceFaultRollback(error)

    def _migrate_disk_resizing_up(self, context, instance, dest, vm_ref,
                                  sr_path):
        self._apply_orig_vm_name_label(instance, vm_ref)

        # 1. Create Snapshot
        label = "%s-snapshot" % instance['name']
        with vm_utils.snapshot_attached_here(
                self._session, instance, vm_ref, label) as vdi_uuids:
            self._update_instance_progress(context, instance,
                                           step=1,
                                           total_steps=RESIZE_TOTAL_STEPS)

            # 2. Transfer the immutable VHDs (base-copies)
            #
            # The first VHD will be the leaf (aka COW) that is being used by
            # the VM. For this step, we're only interested in the immutable
            # VHDs which are all of the parents of the leaf VHD.
            for seq_num, vdi_uuid in itertools.islice(
                    enumerate(vdi_uuids), 1, None):
                self._migrate_vhd(instance, vdi_uuid, dest, sr_path, seq_num)
                self._update_instance_progress(context, instance,
                                               step=2,
                                               total_steps=RESIZE_TOTAL_STEPS)

            # 3. Now power down the instance
            self._resize_ensure_vm_is_shutdown(instance, vm_ref)
            self._update_instance_progress(context, instance,
                                           step=3,
                                           total_steps=RESIZE_TOTAL_STEPS)

            # 4. Transfer the COW VHD
            vdi_ref, vm_vdi_rec = vm_utils.get_vdi_for_vm_safely(
                self._session, vm_ref)
            cow_uuid = vm_vdi_rec['uuid']
            self._migrate_vhd(instance, cow_uuid, dest, sr_path, 0)
            self._update_instance_progress(context, instance,
                                           step=4,
                                           total_steps=RESIZE_TOTAL_STEPS)

    def _apply_orig_vm_name_label(self, instance, vm_ref):
        # NOTE(sirp): in case we're resizing to the same host (for dev
        # purposes), apply a suffix to name-label so the two VM records
        # extant until a confirm_resize don't collide.
        name_label = self._get_orig_vm_name_label(instance)
        vm_utils.set_vm_name_label(self._session, vm_ref, name_label)

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type, block_device_info):
        """Copies a VHD from one host machine to another, possibly
        resizing filesystem before hand.

        :param instance: the instance that owns the VHD in question.
        :param dest: the destination host machine.
        :param instance_type: instance_type to resize to
        """
        # 0. Zero out the progress to begin
        self._update_instance_progress(context, instance,
                                       step=0,
                                       total_steps=RESIZE_TOTAL_STEPS)

        vm_ref = self._get_vm_opaque_ref(instance)
        sr_path = vm_utils.get_sr_path(self._session)

        old_gb = instance['root_gb']
        new_gb = instance_type['root_gb']
        resize_down = old_gb > new_gb

        if resize_down:
            self._migrate_disk_resizing_down(
                    context, instance, dest, instance_type, vm_ref, sr_path)
        else:
            self._migrate_disk_resizing_up(
                    context, instance, dest, vm_ref, sr_path)

        self._detach_block_devices_from_orig_vm(instance, block_device_info)

        # NOTE(sirp): disk_info isn't used by the xenapi driver, instead it
        # uses a staging-area (/images/instance<uuid>) and sequence-numbered
        # VHDs to figure out how to reconstruct the VDI chain after syncing
        disk_info = {}
        return disk_info

    def _detach_block_devices_from_orig_vm(self, instance, block_device_info):
        block_device_mapping = virt_driver.block_device_info_get_mapping(
                block_device_info)
        name_label = self._get_orig_vm_name_label(instance)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mount_device = vol['mount_device'].rpartition("/")[2]
            self._volumeops.detach_volume(connection_info, name_label,
                                          mount_device)

    def _resize_instance(self, instance, root_vdi):
        """Resize an instances root disk."""

        new_disk_size = instance['root_gb'] * 1024 * 1024 * 1024
        if not new_disk_size:
            return

        # Get current size of VDI
        virtual_size = self._session.call_xenapi('VDI.get_virtual_size',
                                                 root_vdi['ref'])
        virtual_size = int(virtual_size)

        old_gb = virtual_size / (1024 * 1024 * 1024)
        new_gb = instance['root_gb']

        if virtual_size < new_disk_size:
            # Resize up. Simple VDI resize will do the trick
            vdi_uuid = root_vdi['uuid']
            LOG.debug(_("Resizing up VDI %(vdi_uuid)s from %(old_gb)dGB to "
                        "%(new_gb)dGB"),
                      {'vdi_uuid': vdi_uuid, 'old_gb': old_gb,
                       'new_gb': new_gb}, instance=instance)
            resize_func_name = self.check_resize_func_name()
            self._session.call_xenapi(resize_func_name, root_vdi['ref'],
                    str(new_disk_size))
            LOG.debug(_("Resize complete"), instance=instance)

    def check_resize_func_name(self):
        """Check the function name used to resize an instance based
        on product_brand and product_version.
        """

        brand = self._session.product_brand
        version = self._session.product_version

        # To maintain backwards compatibility. All recent versions
        # should use VDI.resize
        if bool(version) and bool(brand):
            xcp = brand == 'XCP'
            r1_2_or_above = (
                (
                    version[0] == 1
                    and version[1] > 1
                )
                or version[0] > 1)

            xenserver = brand == 'XenServer'
            r6_or_above = version[0] > 5

            if (xcp and not r1_2_or_above) or (xenserver and not r6_or_above):
                return 'VDI.resize_online'

        return 'VDI.resize'

    def reboot(self, instance, reboot_type, bad_volumes_callback=None):
        """Reboot VM instance."""
        # Note (salvatore-orlando): security group rules are not re-enforced
        # upon reboot, since this action on the XenAPI drivers does not
        # remove existing filters
        vm_ref = self._get_vm_opaque_ref(instance, check_rescue=True)

        try:
            if reboot_type == "HARD":
                self._session.call_xenapi('VM.hard_reboot', vm_ref)
            else:
                self._session.call_xenapi('VM.clean_reboot', vm_ref)
        except self._session.XenAPI.Failure as exc:
            details = exc.details
            if (details[0] == 'VM_BAD_POWER_STATE' and
                    details[-1] == 'halted'):
                LOG.info(_("Starting halted instance found during reboot"),
                    instance=instance)
                self._start(instance, vm_ref=vm_ref,
                            bad_volumes_callback=bad_volumes_callback)
                return
            elif details[0] == 'SR_BACKEND_FAILURE_46':
                LOG.warn(_("Reboot failed due to bad volumes, detaching bad"
                           " volumes and starting halted instance"),
                         instance=instance)
                self._start(instance, vm_ref=vm_ref,
                            bad_volumes_callback=bad_volumes_callback)
                return
            else:
                raise

    def set_admin_password(self, instance, new_pass):
        """Set the root/admin password on the VM instance."""
        if self.agent_enabled(instance):
            vm_ref = self._get_vm_opaque_ref(instance)
            agent = self._get_agent(instance, vm_ref)
            agent.set_admin_password(new_pass)
        else:
            raise NotImplementedError()

    def inject_file(self, instance, path, contents):
        """Write a file to the VM instance."""
        if self.agent_enabled(instance):
            vm_ref = self._get_vm_opaque_ref(instance)
            agent = self._get_agent(instance, vm_ref)
            agent.inject_file(path, contents)
        else:
            raise NotImplementedError()

    @staticmethod
    def _sanitize_xenstore_key(key):
        """
        Xenstore only allows the following characters as keys:

        ABCDEFGHIJKLMNOPQRSTUVWXYZ
        abcdefghijklmnopqrstuvwxyz
        0123456789-/_@

        So convert the others to _

        Also convert / to _, because that is somewhat like a path
        separator.
        """
        allowed_chars = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                         "abcdefghijklmnopqrstuvwxyz"
                         "0123456789-_@")
        return ''.join([x in allowed_chars and x or '_' for x in key])

    def inject_instance_metadata(self, instance, vm_ref):
        """Inject instance metadata into xenstore."""
        @utils.synchronized('xenstore-' + instance['uuid'])
        def store_meta(topdir, data_list):
            for item in data_list:
                key = self._sanitize_xenstore_key(item['key'])
                value = item['value'] or ''
                self._add_to_param_xenstore(vm_ref, '%s/%s' % (topdir, key),
                                            jsonutils.dumps(value))

        # Store user metadata
        store_meta('vm-data/user-metadata', instance['metadata'])

    def inject_auto_disk_config(self, instance, vm_ref):
        """Inject instance's auto_disk_config attribute into xenstore."""
        @utils.synchronized('xenstore-' + instance['uuid'])
        def store_auto_disk_config(key, value):
            value = value and True or False
            self._add_to_param_xenstore(vm_ref, key, str(value))

        store_auto_disk_config('vm-data/auto-disk-config',
                               instance['auto_disk_config'])

    def change_instance_metadata(self, instance, diff):
        """Apply changes to instance metadata to xenstore."""
        vm_ref = self._get_vm_opaque_ref(instance)

        def process_change(location, change):
            if change[0] == '-':
                self._remove_from_param_xenstore(vm_ref, location)
                try:
                    self._delete_from_xenstore(instance, location,
                                               vm_ref=vm_ref)
                except KeyError:
                    # catch KeyError for domid if instance isn't running
                    pass
            elif change[0] == '+':
                self._add_to_param_xenstore(vm_ref, location,
                                            jsonutils.dumps(change[1]))
                try:
                    self._write_to_xenstore(instance, location, change[1],
                                            vm_ref=vm_ref)
                except KeyError:
                    # catch KeyError for domid if instance isn't running
                    pass

        @utils.synchronized('xenstore-' + instance['uuid'])
        def update_meta():
            for key, change in diff.items():
                key = self._sanitize_xenstore_key(key)
                location = 'vm-data/user-metadata/%s' % key
                process_change(location, change)
        update_meta()

    def _find_root_vdi_ref(self, vm_ref):
        """Find and return the root vdi ref for a VM."""
        if not vm_ref:
            return None

        vbd_refs = self._session.call_xenapi("VM.get_VBDs", vm_ref)

        for vbd_uuid in vbd_refs:
            vbd = self._session.call_xenapi("VBD.get_record", vbd_uuid)
            if vbd["userdevice"] == DEVICE_ROOT:
                return vbd["VDI"]

        raise exception.NotFound(_("Unable to find root VBD/VDI for VM"))

    def _destroy_vdis(self, instance, vm_ref):
        """Destroys all VDIs associated with a VM."""
        LOG.debug(_("Destroying VDIs"), instance=instance)

        vdi_refs = vm_utils.lookup_vm_vdis(self._session, vm_ref)
        if not vdi_refs:
            return
        for vdi_ref in vdi_refs:
            try:
                vm_utils.destroy_vdi(self._session, vdi_ref)
            except volume_utils.StorageError as exc:
                LOG.error(exc)

    def _destroy_kernel_ramdisk(self, instance, vm_ref):
        """Three situations can occur:

            1. We have neither a ramdisk nor a kernel, in which case we are a
               RAW image and can omit this step

            2. We have one or the other, in which case, we should flag as an
               error

            3. We have both, in which case we safely remove both the kernel
               and the ramdisk.

        """
        instance_uuid = instance['uuid']
        if not instance['kernel_id'] and not instance['ramdisk_id']:
            # 1. No kernel or ramdisk
            LOG.debug(_("Using RAW or VHD, skipping kernel and ramdisk "
                        "deletion"), instance=instance)
            return

        if not (instance['kernel_id'] and instance['ramdisk_id']):
            # 2. We only have kernel xor ramdisk
            raise exception.InstanceUnacceptable(instance_id=instance_uuid,
               reason=_("instance has a kernel or ramdisk but not both"))

        # 3. We have both kernel and ramdisk
        (kernel, ramdisk) = vm_utils.lookup_kernel_ramdisk(self._session,
                                                           vm_ref)
        if kernel or ramdisk:
            vm_utils.destroy_kernel_ramdisk(self._session, kernel, ramdisk)
            LOG.debug(_("kernel/ramdisk files removed"), instance=instance)

    def _destroy_rescue_instance(self, rescue_vm_ref, original_vm_ref):
        """Destroy a rescue instance."""
        # Shutdown Rescue VM
        vm_rec = self._session.call_xenapi("VM.get_record", rescue_vm_ref)
        state = vm_utils.compile_info(vm_rec)['state']
        if state != power_state.SHUTDOWN:
            self._session.call_xenapi("VM.hard_shutdown", rescue_vm_ref)

        # Destroy Rescue VDIs
        vdi_refs = vm_utils.lookup_vm_vdis(self._session, rescue_vm_ref)
        root_vdi_ref = self._find_root_vdi_ref(original_vm_ref)
        vdi_refs = [vdi_ref for vdi_ref in vdi_refs if vdi_ref != root_vdi_ref]
        vm_utils.safe_destroy_vdis(self._session, vdi_refs)

        # Destroy Rescue VM
        self._session.call_xenapi("VM.destroy", rescue_vm_ref)

    def destroy(self, instance, network_info, block_device_info=None,
                destroy_disks=True):
        """Destroy VM instance.

        This is the method exposed by xenapi_conn.destroy(). The rest of the
        destroy_* methods are internal.

        """
        LOG.info(_("Destroying VM"), instance=instance)

        # We don't use _get_vm_opaque_ref because the instance may
        # truly not exist because of a failure during build. A valid
        # vm_ref is checked correctly where necessary.
        vm_ref = vm_utils.lookup(self._session, instance['name'])

        rescue_vm_ref = vm_utils.lookup(self._session,
                                        "%s-rescue" % instance['name'])
        if rescue_vm_ref:
            self._destroy_rescue_instance(rescue_vm_ref, vm_ref)

        # NOTE(sirp): `block_device_info` is not used, information about which
        # volumes should be detached is determined by the
        # VBD.other_config['osvol'] attribute
        return self._destroy(instance, vm_ref, network_info=network_info,
                             destroy_disks=destroy_disks)

    def _destroy(self, instance, vm_ref, network_info=None,
                 destroy_disks=True):
        """Destroys VM instance by performing:

            1. A shutdown
            2. Destroying associated VDIs.
            3. Destroying kernel and ramdisk files (if necessary).
            4. Destroying that actual VM record.

        """
        if vm_ref is None:
            LOG.warning(_("VM is not present, skipping destroy..."),
                        instance=instance)
            return

        vm_utils.hard_shutdown_vm(self._session, instance, vm_ref)

        if destroy_disks:
            self._volumeops.detach_all(vm_ref)
            self._destroy_vdis(instance, vm_ref)
            self._destroy_kernel_ramdisk(instance, vm_ref)

        vm_utils.destroy_vm(self._session, instance, vm_ref)

        self.unplug_vifs(instance, network_info)
        self.firewall_driver.unfilter_instance(
                instance, network_info=network_info)

    def pause(self, instance):
        """Pause VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._session.call_xenapi('VM.pause', vm_ref)

    def unpause(self, instance):
        """Unpause VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._session.call_xenapi('VM.unpause', vm_ref)

    def suspend(self, instance):
        """Suspend the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._acquire_bootlock(vm_ref)
        self._session.call_xenapi('VM.suspend', vm_ref)

    def resume(self, instance):
        """Resume the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._release_bootlock(vm_ref)
        self._session.call_xenapi('VM.resume', vm_ref, False, True)

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password):
        """Rescue the specified instance.

            - shutdown the instance VM.
            - set 'bootlock' to prevent the instance from starting in rescue.
            - spawn a rescue VM (the vm name-label will be instance-N-rescue).

        """
        rescue_name_label = '%s-rescue' % instance['name']
        rescue_vm_ref = vm_utils.lookup(self._session, rescue_name_label)
        if rescue_vm_ref:
            raise RuntimeError(_("Instance is already in Rescue Mode: %s")
                               % instance['name'])

        vm_ref = self._get_vm_opaque_ref(instance)
        vm_utils.hard_shutdown_vm(self._session, instance, vm_ref)
        self._acquire_bootlock(vm_ref)
        self.spawn(context, instance, image_meta, [], rescue_password,
                   network_info, name_label=rescue_name_label, rescue=True)

    def unrescue(self, instance):
        """Unrescue the specified instance.

            - unplug the instance VM's disk from the rescue VM.
            - teardown the rescue VM.
            - release the bootlock to allow the instance VM to start.

        """
        rescue_vm_ref = vm_utils.lookup(self._session,
                                        "%s-rescue" % instance['name'])
        if not rescue_vm_ref:
            raise exception.InstanceNotInRescueMode(
                    instance_id=instance['uuid'])

        original_vm_ref = self._get_vm_opaque_ref(instance)

        self._destroy_rescue_instance(rescue_vm_ref, original_vm_ref)
        self._release_bootlock(original_vm_ref)
        self._start(instance, original_vm_ref)

    def soft_delete(self, instance):
        """Soft delete the specified instance."""
        try:
            vm_ref = self._get_vm_opaque_ref(instance)
        except exception.NotFound:
            LOG.warning(_("VM is not present, skipping soft delete..."),
                        instance=instance)
        else:
            vm_utils.hard_shutdown_vm(self._session, instance, vm_ref)
            self._acquire_bootlock(vm_ref)

    def restore(self, instance):
        """Restore the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._release_bootlock(vm_ref)
        self._start(instance, vm_ref)

    def power_off(self, instance):
        """Power off the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        vm_utils.hard_shutdown_vm(self._session, instance, vm_ref)

    def power_on(self, instance):
        """Power on the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._start(instance, vm_ref)

    def _cancel_stale_tasks(self, timeout, task):
        """Cancel the given tasks that are older than the given timeout."""
        task_refs = self._session.call_xenapi("task.get_by_name_label", task)
        for task_ref in task_refs:
            task_rec = self._session.call_xenapi("task.get_record", task_ref)
            task_created = timeutils.parse_strtime(task_rec["created"].value,
                                                   "%Y%m%dT%H:%M:%SZ")

            if timeutils.is_older_than(task_created, timeout):
                self._session.call_xenapi("task.cancel", task_ref)

    def poll_rebooting_instances(self, timeout, instances):
        """Look for expirable rebooting instances.

            - issue a "hard" reboot to any instance that has been stuck in a
              reboot state for >= the given timeout
        """
        # NOTE(jk0): All existing clean_reboot tasks must be cancelled before
        # we can kick off the hard_reboot tasks.
        self._cancel_stale_tasks(timeout, 'VM.clean_reboot')

        ctxt = nova_context.get_admin_context()

        instances_info = dict(instance_count=len(instances),
                timeout=timeout)

        if instances_info["instance_count"] > 0:
            LOG.info(_("Found %(instance_count)d hung reboots "
                       "older than %(timeout)d seconds") % instances_info)

        for instance in instances:
            LOG.info(_("Automatically hard rebooting"), instance=instance)
            self.conductor_api.compute_reboot(ctxt, instance, "HARD")

    def get_info(self, instance, vm_ref=None):
        """Return data about VM instance."""
        vm_ref = vm_ref or self._get_vm_opaque_ref(instance)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        return vm_utils.compile_info(vm_rec)

    def get_diagnostics(self, instance):
        """Return data about VM diagnostics."""
        vm_ref = self._get_vm_opaque_ref(instance)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        return vm_utils.compile_diagnostics(vm_rec)

    def _get_vif_device_map(self, vm_rec):
        vif_map = {}
        for vif in [self._session.call_xenapi("VIF.get_record", vrec)
                    for vrec in vm_rec['VIFs']]:
            vif_map[vif['device']] = vif['MAC']
        return vif_map

    def get_all_bw_counters(self):
        """Return running bandwidth counter for each interface on each
           running VM.
        """
        counters = vm_utils.fetch_bandwidth(self._session)
        bw = {}
        for vm_ref, vm_rec in vm_utils.list_vms(self._session):
            vif_map = self._get_vif_device_map(vm_rec)
            name = vm_rec['name_label']
            if 'nova_uuid' not in vm_rec['other_config']:
                continue
            dom = vm_rec.get('domid')
            if dom is None or dom not in counters:
                continue
            vifs_bw = bw.setdefault(name, {})
            for vif_num, vif_data in counters[dom].iteritems():
                mac = vif_map[vif_num]
                vif_data['mac_address'] = mac
                vifs_bw[mac] = vif_data
        return bw

    def get_console_output(self, instance):
        """Return last few lines of instance console."""
        dom_id = self._get_dom_id(instance, check_rescue=True)

        try:
            raw_console_data = self._session.call_plugin('console',
                    'get_console_log', {'dom_id': dom_id})
        except self._session.XenAPI.Failure as exc:
            LOG.exception(exc)
            msg = _("Guest does not have a console available")
            raise exception.NovaException(msg)

        return zlib.decompress(base64.b64decode(raw_console_data))

    def get_vnc_console(self, instance):
        """Return connection info for a vnc console."""
        if instance['vm_state'] == vm_states.RESCUED:
            name = '%s-rescue' % instance['name']
            vm_ref = vm_utils.lookup(self._session, name)
            if vm_ref is None:
                # The rescue instance might not be ready at this point.
                raise exception.InstanceNotReady(instance_id=instance['uuid'])
        else:
            vm_ref = vm_utils.lookup(self._session, instance['name'])
            if vm_ref is None:
                # The compute manager expects InstanceNotFound for this case.
                raise exception.InstanceNotFound(instance_id=instance['uuid'])

        session_id = self._session.get_session_id()
        path = "/console?ref=%s&session_id=%s" % (str(vm_ref), session_id)

        # NOTE: XS5.6sp2+ use http over port 80 for xenapi com
        return {'host': CONF.vncserver_proxyclient_address, 'port': 80,
                'internal_access_path': path}

    def _vif_xenstore_data(self, vif):
        """convert a network info vif to injectable instance data."""

        def get_ip(ip):
            if not ip:
                return None
            return ip['address']

        def fixed_ip_dict(ip, subnet):
            if ip['version'] == 4:
                netmask = str(subnet.as_netaddr().netmask)
            else:
                netmask = subnet.as_netaddr()._prefixlen

            return {'ip': ip['address'],
                    'enabled': '1',
                    'netmask': netmask,
                    'gateway': get_ip(subnet['gateway'])}

        def convert_route(route):
            return {'route': str(netaddr.IPNetwork(route['cidr']).network),
                    'netmask': str(netaddr.IPNetwork(route['cidr']).netmask),
                    'gateway': get_ip(route['gateway'])}

        network = vif['network']
        v4_subnets = [subnet for subnet in network['subnets']
                             if subnet['version'] == 4]
        v6_subnets = [subnet for subnet in network['subnets']
                             if subnet['version'] == 6]

        # NOTE(tr3buchet): routes and DNS come from all subnets
        routes = [convert_route(route) for subnet in network['subnets']
                                       for route in subnet['routes']]
        dns = [get_ip(ip) for subnet in network['subnets']
                          for ip in subnet['dns']]

        info_dict = {'label': network['label'],
                     'mac': vif['address']}

        if v4_subnets:
            # NOTE(tr3buchet): gateway and broadcast from first subnet
            #                  primary IP will be from first subnet
            #                  subnets are generally unordered :(
            info_dict['gateway'] = get_ip(v4_subnets[0]['gateway'])
            info_dict['broadcast'] = str(v4_subnets[0].as_netaddr().broadcast)
            info_dict['ips'] = [fixed_ip_dict(ip, subnet)
                                for subnet in v4_subnets
                                for ip in subnet['ips']]
        if v6_subnets:
            # NOTE(tr3buchet): gateway from first subnet
            #                  primary IP will be from first subnet
            #                  subnets are generally unordered :(
            info_dict['gateway_v6'] = get_ip(v6_subnets[0]['gateway'])
            info_dict['ip6s'] = [fixed_ip_dict(ip, subnet)
                                 for subnet in v6_subnets
                                 for ip in subnet['ips']]
        if routes:
            info_dict['routes'] = routes

        if dns:
            info_dict['dns'] = list(set(dns))

        return info_dict

    def inject_network_info(self, instance, network_info, vm_ref=None):
        """
        Generate the network info and make calls to place it into the
        xenstore and the xenstore param list.
        vm_ref can be passed in because it will sometimes be different than
        what vm_utils.lookup(session, instance['name']) will find (ex: rescue)
        """
        vm_ref = vm_ref or self._get_vm_opaque_ref(instance)
        LOG.debug(_("Injecting network info to xenstore"), instance=instance)

        @utils.synchronized('xenstore-' + instance['uuid'])
        def update_nwinfo():
            for vif in network_info:
                xs_data = self._vif_xenstore_data(vif)
                location = ('vm-data/networking/%s' %
                            vif['address'].replace(':', ''))
                self._add_to_param_xenstore(vm_ref,
                                            location,
                                            jsonutils.dumps(xs_data))
                try:
                    self._write_to_xenstore(instance, location, xs_data,
                                            vm_ref=vm_ref)
                except KeyError:
                    # catch KeyError for domid if instance isn't running
                    pass
        update_nwinfo()

    def _create_vifs(self, vm_ref, instance, network_info):
        """Creates vifs for an instance."""

        LOG.debug(_("Creating vifs"), instance=instance)

        # this function raises if vm_ref is not a vm_opaque_ref
        self._session.call_xenapi("VM.get_record", vm_ref)

        for device, vif in enumerate(network_info):
            vif_rec = self.vif_driver.plug(instance, vif,
                                           vm_ref=vm_ref, device=device)
            network_ref = vif_rec['network']
            LOG.debug(_('Creating VIF for network %s'),
                      network_ref, instance=instance)
            vif_ref = self._session.call_xenapi('VIF.create', vif_rec)
            LOG.debug(_('Created VIF %(vif_ref)s, network %(network_ref)s'),
                      {'vif_ref': vif_ref, 'network_ref': network_ref},
                      instance=instance)

    def plug_vifs(self, instance, network_info):
        """Set up VIF networking on the host."""
        for device, vif in enumerate(network_info):
            self.vif_driver.plug(instance, vif, device=device)

    def unplug_vifs(self, instance, network_info):
        if network_info:
            for vif in network_info:
                self.vif_driver.unplug(instance, vif)

    def reset_network(self, instance):
        """Calls resetnetwork method in agent."""
        if self.agent_enabled(instance):
            vm_ref = self._get_vm_opaque_ref(instance)
            agent = self._get_agent(instance, vm_ref)
            agent.resetnetwork()
        else:
            raise NotImplementedError()

    def inject_hostname(self, instance, vm_ref, hostname):
        """Inject the hostname of the instance into the xenstore."""
        if instance['os_type'] == "windows":
            # NOTE(jk0): Windows hostnames can only be <= 15 chars.
            hostname = hostname[:15]

        LOG.debug(_("Injecting hostname to xenstore"), instance=instance)
        self._add_to_param_xenstore(vm_ref, 'vm-data/hostname', hostname)

    def remove_hostname(self, instance, vm_ref):
        LOG.debug(_("Removing hostname from xenstore"), instance=instance)
        self._remove_from_param_xenstore(vm_ref, 'vm-data/hostname')

    def _write_to_xenstore(self, instance, path, value, vm_ref=None):
        """
        Writes the passed value to the xenstore record for the given VM
        at the specified location. A XenAPIPlugin.PluginError will be raised
        if any error is encountered in the write process.
        """
        return self._make_plugin_call('xenstore.py', 'write_record', instance,
                                      vm_ref=vm_ref, path=path,
                                      value=jsonutils.dumps(value))

    def _delete_from_xenstore(self, instance, path, vm_ref=None):
        """
        Deletes the value from the xenstore record for the given VM at
        the specified location.  A XenAPIPlugin.PluginError will be
        raised if any error is encountered in the delete process.
        """
        return self._make_plugin_call('xenstore.py', 'delete_record', instance,
                                      vm_ref=vm_ref, path=path)

    def _make_plugin_call(self, plugin, method, instance=None, vm_ref=None,
                          **addl_args):
        """
        Abstracts out the process of calling a method of a xenapi plugin.
        Any errors raised by the plugin will in turn raise a RuntimeError here.
        """
        args = {}
        if instance or vm_ref:
            args['dom_id'] = self._get_dom_id(instance, vm_ref)
        args.update(addl_args)
        try:
            return self._session.call_plugin(plugin, method, args)
        except self._session.XenAPI.Failure as e:
            err_msg = e.details[-1].splitlines()[-1]
            if 'TIMEOUT:' in err_msg:
                LOG.error(_('TIMEOUT: The call to %(method)s timed out. '
                            'args=%(args)r'),
                          {'method': method, 'args': args}, instance=instance)
                return {'returncode': 'timeout', 'message': err_msg}
            elif 'NOT IMPLEMENTED:' in err_msg:
                LOG.error(_('NOT IMPLEMENTED: The call to %(method)s is not'
                            ' supported by the agent. args=%(args)r'),
                          {'method': method, 'args': args}, instance=instance)
                return {'returncode': 'notimplemented', 'message': err_msg}
            else:
                LOG.error(_('The call to %(method)s returned an error: %(e)s. '
                            'args=%(args)r'),
                          {'method': method, 'args': args, 'e': e},
                          instance=instance)
                return {'returncode': 'error', 'message': err_msg}
            return None

    def _get_dom_id(self, instance=None, vm_ref=None, check_rescue=False):
        vm_ref = vm_ref or self._get_vm_opaque_ref(instance, check_rescue)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        return vm_rec['domid']

    def _add_to_param_xenstore(self, vm_ref, key, val):
        """
        Takes a key/value pair and adds it to the xenstore parameter
        record for the given vm instance. If the key exists in xenstore,
        it is overwritten
        """
        self._remove_from_param_xenstore(vm_ref, key)
        self._session.call_xenapi('VM.add_to_xenstore_data', vm_ref, key, val)

    def _remove_from_param_xenstore(self, vm_ref, key):
        """
        Takes a single key and removes it from the xenstore parameter
        record data for the given VM.
        If the key doesn't exist, the request is ignored.
        """
        self._session.call_xenapi('VM.remove_from_xenstore_data', vm_ref, key)

    def refresh_security_group_rules(self, security_group_id):
        """recreates security group rules for every instance."""
        self.firewall_driver.refresh_security_group_rules(security_group_id)

    def refresh_security_group_members(self, security_group_id):
        """recreates security group rules for every instance."""
        self.firewall_driver.refresh_security_group_members(security_group_id)

    def refresh_instance_security_rules(self, instance):
        """recreates security group rules for specified instance."""
        self.firewall_driver.refresh_instance_security_rules(instance)

    def refresh_provider_fw_rules(self):
        self.firewall_driver.refresh_provider_fw_rules()

    def unfilter_instance(self, instance_ref, network_info):
        """Removes filters for each VIF of the specified instance."""
        self.firewall_driver.unfilter_instance(instance_ref,
                                               network_info=network_info)

    def _get_host_uuid_from_aggregate(self, context, hostname):
        current_aggregate = self._virtapi.aggregate_get_by_host(
            context, CONF.host, key=pool_states.POOL_FLAG)[0]
        if not current_aggregate:
            raise exception.AggregateHostNotFound(host=CONF.host)
        try:
            return current_aggregate.metadetails[hostname]
        except KeyError:
            reason = _('Destination host:%s must be in the same '
                       'aggregate as the source server') % hostname
            raise exception.MigrationPreCheckError(reason=reason)

    def _ensure_host_in_aggregate(self, context, hostname):
        self._get_host_uuid_from_aggregate(context, hostname)

    def _get_host_opaque_ref(self, context, hostname):
        host_uuid = self._get_host_uuid_from_aggregate(context, hostname)
        return self._session.call_xenapi("host.get_by_uuid", host_uuid)

    def _migrate_receive(self, ctxt):
        destref = self._session.get_xenapi_host()
        # Get the network to for migrate.
        # This is the one associated with the pif marked management. From cli:
        # uuid=`xe pif-list --minimal management=true`
        # xe pif-param-get param-name=network-uuid uuid=$uuid
        expr = 'field "management" = "true"'
        pifs = self._session.call_xenapi('PIF.get_all_records_where',
                                         expr)
        if len(pifs) != 1:
            msg = _('No suitable network for migrate')
            raise exception.MigrationPreCheckError(reason=msg)

        nwref = pifs[pifs.keys()[0]]['network']
        try:
            options = {}
            migrate_data = self._session.call_xenapi("host.migrate_receive",
                                                     destref,
                                                     nwref,
                                                     options)
        except self._session.XenAPI.Failure as exc:
            LOG.exception(exc)
            msg = _('Migrate Receive failed')
            raise exception.MigrationPreCheckError(reason=msg)
        return migrate_data

    def _get_iscsi_srs(self, ctxt, instance_ref):
        vm_ref = self._get_vm_opaque_ref(instance_ref)
        vbd_refs = self._session.call_xenapi("VM.get_VBDs", vm_ref)

        iscsi_srs = []

        for vbd_ref in vbd_refs:
            vdi_ref = self._session.call_xenapi("VBD.get_VDI", vbd_ref)
            # Check if it's on an iSCSI SR
            sr_ref = self._session.call_xenapi("VDI.get_SR", vdi_ref)
            if self._session.call_xenapi("SR.get_type", sr_ref) == 'iscsi':
                iscsi_srs.append(sr_ref)

        return iscsi_srs

    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit

        """
        dest_check_data = {}
        if block_migration:
            migrate_send_data = self._migrate_receive(ctxt)
            destination_sr_ref = vm_utils.safe_find_sr(self._session)
            dest_check_data.update(
                {"block_migration": block_migration,
                 "migrate_data": {"migrate_send_data": migrate_send_data,
                                  "destination_sr_ref": destination_sr_ref}})
        else:
            src = instance_ref['host']
            self._ensure_host_in_aggregate(ctxt, src)
            # TODO(johngarbutt) we currently assume
            # instance is on a SR shared with other destination
            # block migration work will be able to resolve this
        return dest_check_data

    def _is_xsm_sr_check_relaxed(self):
        try:
            return self.cached_xsm_sr_relaxed
        except AttributeError:
            config_value = None
            try:
                config_value = self._make_plugin_call('config_file',
                                                      'get_val',
                                                      key='relax-xsm-sr-check')
            except Exception as exc:
                LOG.exception(exc)
            self.cached_xsm_sr_relaxed = config_value == "true"
            return self.cached_xsm_sr_relaxed

    def check_can_live_migrate_source(self, ctxt, instance_ref,
                                      dest_check_data):
        """Check if it's possible to execute live migration on the source side.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance object
        :param dest_check_data: data returned by the check on the
                                destination, includes block_migration flag

        """
        if len(self._get_iscsi_srs(ctxt, instance_ref)) > 0:
            # XAPI must support the relaxed SR check for live migrating with
            # iSCSI VBDs
            if not self._is_xsm_sr_check_relaxed():
                raise exception.MigrationError(_('XAPI supporting '
                                'relax-xsm-sr-check=true requried'))

        if 'migrate_data' in dest_check_data:
            vm_ref = self._get_vm_opaque_ref(instance_ref)
            migrate_data = dest_check_data['migrate_data']
            try:
                self._call_live_migrate_command(
                    "VM.assert_can_migrate", vm_ref, migrate_data)
            except self._session.XenAPI.Failure as exc:
                LOG.exception(exc)
                msg = _('VM.assert_can_migrate failed')
                raise exception.MigrationPreCheckError(reason=msg)
        return dest_check_data

    def _generate_vdi_map(self, destination_sr_ref, vm_ref, sr_ref=None):
        """generate a vdi_map for _call_live_migrate_command."""
        if sr_ref is None:
            sr_ref = vm_utils.safe_find_sr(self._session)
        vm_vdis = vm_utils.get_instance_vdis_for_sr(self._session,
                                                    vm_ref, sr_ref)
        return dict((vdi, destination_sr_ref) for vdi in vm_vdis)

    def _call_live_migrate_command(self, command_name, vm_ref, migrate_data):
        """unpack xapi specific parameters, and call a live migrate command."""
        destination_sr_ref = migrate_data['destination_sr_ref']
        migrate_send_data = migrate_data['migrate_send_data']

        vdi_map = self._generate_vdi_map(destination_sr_ref, vm_ref)

        # Add destination SR refs for all of the VDIs that we created
        # as part of the pre migration callback
        if 'pre_live_migration_result' in migrate_data:
            pre_migrate_data = migrate_data['pre_live_migration_result']
            sr_uuid_map = pre_migrate_data.get('sr_uuid_map', [])
            for sr_uuid in sr_uuid_map:
                # Source and destination SRs have the same UUID, so get the
                # reference for the local SR
                sr_ref = self._session.call_xenapi("SR.get_by_uuid", sr_uuid)
                vdi_map.update(
                    self._generate_vdi_map(
                        sr_uuid_map[sr_uuid], vm_ref, sr_ref))
        vif_map = {}
        options = {}
        self._session.call_xenapi(command_name, vm_ref,
                                  migrate_send_data, True,
                                  vdi_map, vif_map, options)

    def live_migrate(self, context, instance, destination_hostname,
                     post_method, recover_method, block_migration,
                     migrate_data=None):
        try:
            vm_ref = self._get_vm_opaque_ref(instance)
            if block_migration:
                if not migrate_data:
                    raise exception.InvalidParameterValue('Block Migration '
                                    'requires migrate data from destination')

                iscsi_srs = self._get_iscsi_srs(context, instance)
                try:
                    self._call_live_migrate_command(
                        "VM.migrate_send", vm_ref, migrate_data)
                except self._session.XenAPI.Failure as exc:
                    LOG.exception(exc)
                    raise exception.MigrationError(_('Migrate Send failed'))

                # Tidy up the iSCSI SRs
                for sr_ref in iscsi_srs:
                    volume_utils.forget_sr(self._session, sr_ref)
            else:
                host_ref = self._get_host_opaque_ref(context,
                                                     destination_hostname)
                self._session.call_xenapi("VM.pool_migrate", vm_ref,
                                          host_ref, {})
            post_method(context, instance, destination_hostname,
                        block_migration)
        except Exception:
            with excutils.save_and_reraise_exception():
                recover_method(context, instance, destination_hostname,
                               block_migration)

    def get_per_instance_usage(self):
        """Get usage info about each active instance."""
        usage = {}

        def _is_active(vm_rec):
            power_state = vm_rec['power_state'].lower()
            return power_state in ['running', 'paused']

        def _get_uuid(vm_rec):
            other_config = vm_rec['other_config']
            return other_config.get('nova_uuid', None)

        for vm_ref, vm_rec in vm_utils.list_vms(self._session):
            uuid = _get_uuid(vm_rec)

            if _is_active(vm_rec) and uuid is not None:
                memory_mb = int(vm_rec['memory_static_max']) / 1024 / 1024
                usage[uuid] = {'memory_mb': memory_mb, 'uuid': uuid}

        return usage

    def attach_block_device_volumes(self, block_device_info):
        sr_uuid_map = {}
        try:
            if block_device_info is not None:
                for block_device_map in block_device_info[
                                                'block_device_mapping']:
                    sr_uuid, _ = self._volumeops.attach_volume(
                        block_device_map['connection_info'],
                        None,
                        block_device_map['mount_device'],
                        hotplug=False)

                    sr_ref = self._session.call_xenapi('SR.get_by_uuid',
                                                       sr_uuid)
                    sr_uuid_map[sr_uuid] = sr_ref
        except Exception:
            with excutils.save_and_reraise_exception():
                # Disconnect the volumes we just connected
                for sr in sr_uuid_map:
                    volume_utils.forget_sr(self._session, sr_uuid_map[sr_ref])

        return sr_uuid_map
