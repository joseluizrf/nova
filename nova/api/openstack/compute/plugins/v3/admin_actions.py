#   Copyright 2011 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

import os.path
import traceback

import webob
from webob import exc

from nova.api.openstack import common
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import compute
from nova.compute import vm_states
from nova import exception
from nova.openstack.common.gettextutils import _
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
ALIAS = "os-admin-actions"

# States usable in resetState action
state_map = dict(active=vm_states.ACTIVE, error=vm_states.ERROR)


def authorize(context, action_name):
    action = 'v3:%s:%s' % (ALIAS, action_name)
    extensions.extension_authorizer('compute', action)(context)


class AdminActionsController(wsgi.Controller):
    def __init__(self, *args, **kwargs):
        super(AdminActionsController, self).__init__(*args, **kwargs)
        self.compute_api = compute.API()

    @wsgi.action('pause')
    def _pause(self, req, id, body):
        """Permit Admins to pause the server."""
        ctxt = req.environ['nova.context']
        authorize(ctxt, 'pause')
        try:
            server = self.compute_api.get(ctxt, id)
            self.compute_api.pause(ctxt, server)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'pause')
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::pause %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('unpause')
    def _unpause(self, req, id, body):
        """Permit Admins to unpause the server."""
        ctxt = req.environ['nova.context']
        authorize(ctxt, 'unpause')
        try:
            server = self.compute_api.get(ctxt, id)
            self.compute_api.unpause(ctxt, server)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'unpause')
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::unpause %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('suspend')
    def _suspend(self, req, id, body):
        """Permit admins to suspend the server."""
        context = req.environ['nova.context']
        authorize(context, 'suspend')
        try:
            server = self.compute_api.get(context, id, want_objects=True)
            self.compute_api.suspend(context, server)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'suspend')
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("compute.api::suspend %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('resume')
    def _resume(self, req, id, body):
        """Permit admins to resume the server from suspend."""
        context = req.environ['nova.context']
        authorize(context, 'resume')
        try:
            server = self.compute_api.get(context, id, want_objects=True)
            self.compute_api.resume(context, server)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'resume')
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("compute.api::resume %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('migrate')
    def _migrate(self, req, id, body):
        """Permit admins to migrate a server to a new host."""
        context = req.environ['nova.context']
        authorize(context, 'migrate')
        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.resize(req.environ['nova.context'], instance)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'migrate')
        except Exception as e:
            LOG.exception(_("Error in migrate %s"), e)
            raise exc.HTTPBadRequest()
        return webob.Response(status_int=202)

    @wsgi.action('reset_network')
    def _reset_network(self, req, id, body):
        """Permit admins to reset networking on a server."""
        context = req.environ['nova.context']
        authorize(context, 'reset_network')
        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.reset_network(context, instance)
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::reset_network %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('inject_network_info')
    def _inject_network_info(self, req, id, body):
        """Permit admins to inject network info into a server."""
        context = req.environ['nova.context']
        authorize(context, 'inject_network_info')
        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.inject_network_info(context, instance)
        except exception.InstanceNotFound:
            raise exc.HTTPNotFound(_("Server not found"))
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::inject_network_info %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('lock')
    def _lock(self, req, id, body):
        """Permit admins to lock a server."""
        context = req.environ['nova.context']
        authorize(context, 'lock')
        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.lock(context, instance)
        except exception.InstanceNotFound:
            raise exc.HTTPNotFound(_("Server not found"))
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::lock %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('unlock')
    def _unlock(self, req, id, body):
        """Permit admins to lock a server."""
        context = req.environ['nova.context']
        authorize(context, 'unlock')
        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.unlock(context, instance)
        except exception.InstanceNotFound:
            raise exc.HTTPNotFound(_("Server not found"))
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::unlock %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)

    @wsgi.action('create_backup')
    def _create_backup(self, req, id, body):
        """Backup a server instance.

        Images now have an `image_type` associated with them, which can be
        'snapshot' or the backup type, like 'daily' or 'weekly'.

        If the image_type is backup-like, then the rotation factor can be
        included and that will cause the oldest backups that exceed the
        rotation factor to be deleted.

        """
        context = req.environ["nova.context"]
        authorize(context, 'create_backup')

        try:
            entity = body["create_backup"]
        except (KeyError, TypeError):
            raise exc.HTTPBadRequest(_("Malformed request body"))

        try:
            image_name = entity["name"]
            backup_type = entity["backup_type"]
            rotation = entity["rotation"]

        except KeyError as missing_key:
            msg = _("create_backup entity requires %s attribute") % missing_key
            raise exc.HTTPBadRequest(explanation=msg)

        except TypeError:
            msg = _("Malformed create_backup entity")
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            rotation = int(rotation)
        except ValueError:
            msg = _("create_backup attribute 'rotation' must be an integer")
            raise exc.HTTPBadRequest(explanation=msg)
        if rotation < 0:
            msg = _("create_backup attribute 'rotation' must be greater "
                    "than or equal to zero")
            raise exc.HTTPBadRequest(explanation=msg)

        props = {}
        metadata = entity.get('metadata', {})
        common.check_img_metadata_properties_quota(context, metadata)
        try:
            props.update(metadata)
        except ValueError:
            msg = _("Invalid metadata")
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            instance = self.compute_api.get(context, id)
        except exception.NotFound:
            raise exc.HTTPNotFound(_("Instance not found"))

        try:
            image = self.compute_api.backup(context, instance, image_name,
                    backup_type, rotation, extra_properties=props)
        except exception.InstanceInvalidState as state_error:
            common.raise_http_conflict_for_instance_invalid_state(state_error,
                    'create_backup')

        resp = webob.Response(status_int=202)

        # build location of newly-created image entity if rotation is not zero
        if rotation > 0:
            image_id = str(image['id'])
            image_ref = os.path.join(req.application_url, 'images', image_id)
            resp.headers['Location'] = image_ref

        return resp

    @wsgi.action('migrate_live')
    def _migrate_live(self, req, id, body):
        """Permit admins to (live) migrate a server to a new host."""
        context = req.environ["nova.context"]
        authorize(context, 'migrate_live')

        try:
            block_migration = body["migrate_live"]["block_migration"]
            disk_over_commit = body["migrate_live"]["disk_over_commit"]
            host = body["migrate_live"]["host"]
        except (TypeError, KeyError):
            msg = _("host, block_migration and disk_over_commit must "
                    "be specified for live migration.")
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            instance = self.compute_api.get(context, id)
            self.compute_api.live_migrate(context, instance, block_migration,
                                          disk_over_commit, host)
        except (exception.ComputeServiceUnavailable,
                exception.InvalidHypervisorType,
                exception.UnableToMigrateToSelf,
                exception.DestinationHypervisorTooOld) as ex:
            raise exc.HTTPBadRequest(explanation=ex.format_message())
        except Exception:
            if host is None:
                msg = _("Live migration of instance %s to another host "
                        "failed") % id
            else:
                msg = _("Live migration of instance %(id)s to host %(host)s "
                        "failed") % {'id': id, 'host': host}
            LOG.exception(msg)
            # Return messages from scheduler
            raise exc.HTTPBadRequest(explanation=msg)

        return webob.Response(status_int=202)

    @wsgi.action('reset_state')
    def _reset_state(self, req, id, body):
        """Permit admins to reset the state of a server."""
        context = req.environ["nova.context"]
        authorize(context, 'reset_state')

        # Identify the desired state from the body
        try:
            state = state_map[body["reset_state"]["state"]]
        except (TypeError, KeyError):
            msg = _("Desired state must be specified.  Valid states "
                    "are: %s") % ', '.join(sorted(state_map.keys()))
            raise exc.HTTPBadRequest(explanation=msg)

        try:
            instance = self.compute_api.get(context, id, want_objects=True)
            instance.vm_state = state
            instance.task_state = None
            instance.save(admin_state_reset=True)
        except exception.InstanceNotFound:
            raise exc.HTTPNotFound(_("Server not found"))
        except Exception:
            readable = traceback.format_exc()
            LOG.exception(_("Compute.api::reset_state %s"), readable)
            raise exc.HTTPUnprocessableEntity()
        return webob.Response(status_int=202)


class AdminActions(extensions.V3APIExtensionBase):
    """Enable admin-only server actions

    Actions include: pause, unpause, suspend, resume, migrate,
    reset_network, inject_network_info, lock, unlock, create_backup
    """

    name = "AdminActions"
    alias = ALIAS
    namespace = "http://docs.openstack.org/compute/ext/%s/api/v3" % ALIAS
    version = 1

    def get_controller_extensions(self):
        controller = AdminActionsController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]

    def get_resources(self):
        return []
