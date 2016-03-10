"""-------------------------------------------------------------------------
Copyright IBM Corp. 2015, 2015 All Rights Reserved
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
Limitations under the License.
-------------------------------------------------------------------------"""

"""
Created on Feb 18, 2014
@author: Gil Vernik
"""


import ConfigParser
import urllib
from eventlet import Timeout
from six.moves.urllib.parse import quote
from storlet_common import StorletTimeout
from swift.common.constraints import check_copy_from_header, \
    check_destination_header
from swift.common.exceptions import ConnectionTimeout
from swift.common.swob import HTTPBadRequest, HTTPException, \
    HTTPInternalServerError, HTTPMethodNotAllowed, HTTPPreconditionFailed, \
    wsgify
from swift.common.utils import config_true_value, get_logger, is_success, \
    register_swift_info
from swift.proxy.controllers.base import get_account_info


class NotStorletRequest(Exception):
    pass


def _request_instance_property():
    """
    Set and retrieve the request instance.
    This works to force to tie the consistency between the request path and
    self.vars (i.e. api_version, account, container, obj) even if unexpectedly
    (separately) assigned.
    """
    def getter(self):
        return self._request

    def setter(self, request):
        self._request = request
        try:
            self._extract_vaco()
        except ValueError:
            raise NotStorletRequest()

    return property(getter, setter,
                    doc="Force to tie the request to acc/con/obj vars")


class BaseStorletHandler(object):
    """
    This is an abstract handler for Proxy/Object Server middleware
    """
    request = _request_instance_property()

    def __init__(self, request, conf, app, logger):
        """
        :param request: swob.Request instance
        :param conf: gatway conf dict
        """
        self.request = request
        self.storlet_containers = [conf.get('storlet_container'),
                                   conf.get('storlet_dependency')]
        self.app = app
        self.logger = logger
        self.conf = conf

    def _setup_gateway(self):
        gateway_class = self.conf['gateway_module']
        self.gateway = gateway_class(
            self.conf, self.logger, self.app, self.api_version,
            self.account, self.container, self.obj)
        self._update_storlet_parameters_from_headers()

    def _extract_vaco(self):
        """
        Set version, account, container, obj vars from self._parse_vaco result

        :raises ValueError: if self._parse_vaco raises ValueError while
                            parsing, this method doesn't care and raise it to
                            upper caller.
        """
        self._api_version, self._account, self._container, self._obj = \
            self._parse_vaco()

    @property
    def api_version(self):
        return self._api_version

    @property
    def account(self):
        return self._account

    @property
    def container(self):
        return self._container

    @property
    def obj(self):
        return self._obj

    def _parse_vaco(self):
        """
        Parse method of path from self.request which depends on child class
        (Proxy or Object)
        :return tuple: a string tuple of (version, account, container, object)
        """
        raise NotImplementedError()

    def handle_request(self):
        """
        Run storlet
        """
        raise NotImplementedError()

    @property
    def is_storlet_execution(self):
        return 'X-Run-Storlet' in self.request.headers

    @property
    def is_range_request(self):
        """
        Determines whether the request is a byte-range request
        """
        return 'Range' in self.request.headers

    def is_slo_response(self, resp):
        self.logger.debug(
            'Verify if {0}/{1}/{2} is an SLO assembly object'.format(
                self.account, self.container, self.obj))
        is_slo = 'X-Static-Large-Object' in resp.headers
        if is_slo:
            self.logger.debug(
                '{0}/{1}/{2} is indeed an SLO assembly '
                'object'.format(self.account, self.container, self.obj))
        else:
            self.logger.debug(
                '{0}/{1}/{2} is NOT an SLO assembly object'.format(
                    self.account, self.container, self.obj))
        return is_slo

    def _update_storlet_parameters_from_headers(self):
        """
        Extract parameters for header (an alternative to parmeters through
        the query string)

        """
        parameters = {}
        for param in self.request.headers:
            if param.lower().startswith('x-storlet-parameter'):
                keyvalue = self.request.headers[param]
                keyvalue = urllib.unquote(keyvalue)
                [key, value] = keyvalue.split(':')
                parameters[key] = value
        self.request.params.update(parameters)

    def _call_gateway(self, resp):
        """
        Call gateway module to get result of storlet execution
        in GET flow
        """
        raise NotImplementedError()

    def apply_storlet(self, resp):
        outmd, app_iter = self._call_gateway(resp)

        if 'Content-Length' in resp.headers:
            resp.headers.pop('Content-Length')
        if 'Transfer-Encoding' in resp.headers:
            resp.headers.pop('Transfer-Encoding')
        resp.app_iter = app_iter
        return resp


class StorletProxyHandler(BaseStorletHandler):
    def __init__(self, request, conf, app, logger):
        super(StorletProxyHandler, self).__init__(
            request, conf, app, logger)

        # proxy need the gateway module both execution and storlet object
        if (self.is_storlet_execution or self.is_storlet_object_update):
            # In proxy server, storlet handler validate if storlet enabled
            # at the account, anyway
            account_meta = get_account_info(self.request.environ,
                                            self.app)['meta']
            storlets_enabled = account_meta.get('storlet-enabled',
                                                'False')
            if not config_true_value(storlets_enabled):
                self.logger.debug('Account disabled for storlets')
                raise HTTPBadRequest('Account disabled for storlets',
                                     request=self.request)

            self._setup_gateway()
        else:
            # others are not storlet request
            raise NotStorletRequest()

    def _parse_vaco(self):
        return self.request.split_path(4, 4, rest_with_last=True)

    def is_proxy_runnable(self, resp):
        # SLO / proxy only case:
        # storlet to be invoked now at proxy side:
        runnable = any(
            [self.is_range_request, self.is_slo_response(resp),
             self.conf['storlet_execute_on_proxy_only']])
        return runnable

    @property
    def is_storlet_object_update(self):
        return (self.container in self.storlet_containers and self.obj
                and self.request.method in ['PUT', 'POST'])

    @property
    def is_put_copy_request(self):
        return 'X-Copy-From' in self.request.headers

    def handle_request(self):
        if self.is_storlet_object_update:
            # TODO(takashi): We have to validate metadata in COPY case
            self.gateway.validateStorletUpload(self.request)
            return self.request.get_response(self.app)
        else:
            if hasattr(self, self.request.method):
                resp = getattr(self, self.request.method)()
                return resp
            else:
                raise HTTPMethodNotAllowed(req=self.request)

    def _call_gateway(self, resp):
        return self.gateway.gatewayProxyGetFlow(
            self.request, self.container, self.obj, resp)

    def GET(self):
        """
        GET handler on Proxy
        """
        self.gateway.authorizeStorletExecution(self.request)

        # The get request may be a SLO object GET request.
        # Simplest solution would be to invoke a HEAD
        # for every GET request to test if we are in SLO case.
        # In order to save the HEAD overhead we implemented
        # a slightly more involved flow:
        # At proxy side, we augment request with Storlet stuff
        # and let the request flow.
        # At object side, we invoke the plain (non Storlet)
        # request and test if we are in SLO case.
        # and invoke Storlet only if non SLO case.
        # Back at proxy side, we test if test received
        # full object to detect if we are in SLO case,
        # and invoke Storlet only if in SLO case.
        self.gateway.augmentStorletRequest(self.request)
        original_resp = self.request.get_response(self.app)

        if original_resp.is_success:
            if self.is_proxy_runnable(original_resp):
                return self.apply_storlet(original_resp)
            else:
                # Non proxy GET case: Storlet was already invoked at
                # object side
                # TODO(kota_): Do we need to pop the Transfer-Encoding/
                #              Content-Length header from the resp?
                if 'Transfer-Encoding' in original_resp.headers:
                    original_resp.headers.pop('Transfer-Encoding')

                original_resp.headers['Content-Length'] = None
                return original_resp

        else:
            # In failure case, we need nothing to do, just return original
            # response
            return original_resp

    def _validate_copy_request(self):
        # We currently block copy from account
        unsupported_headers = ['X-Copy-From-Account',
                               'Destination-Account',
                               'X-Fresh-Metadata']

        for header in unsupported_headers:
            if self.request.headers.get(header):
                raise HTTPBadRequest(
                    'Storlet on copy with %s is not supported' %
                    header)

    def handle_put_copy_response(self, out_md, app_iter):
        self.request.environ['wsgi.input'] = app_iter
        if 'CONTENT_LENGTH' in self.request.environ:
            self.request.environ.pop('CONTENT_LENGTH')
        self.request.headers['Transfer-Encoding'] = 'chunked'
        return self.request.get_response(self.app)

    def base_handle_copy_request(self, src_container, src_obj,
                                 dest_container, dest_object):
        """
        Unified path for:
        PUT verb with X-Copy-From and
        COPY verb with Destination
        """
        # Get an iterator over the source object
        source_path = '/%s/%s/%s/%s' % (self.api_version, self.account,
                                        src_container, src_obj)
        source_req = self.request.copy_get()
        source_req.headers.pop('X-Backend-Storage-Policy-Index', None)
        source_req.headers.pop('X-Run-Storlet', None)
        source_req.path_info = source_path
        source_req.headers['X-Newest'] = 'true'

        source_resp = source_req.get_response(self.app)

        # Do proxy copy flow
        (out_md, app_iter) = self.gateway.gatewayProxyCopyFlow(self.request,
                                                               dest_container,
                                                               dest_object,
                                                               source_resp)

        resp = self.handle_put_copy_response(out_md, app_iter)
        acct, path = source_resp.environ['PATH_INFO'].split('/', 3)[2:4]
        resp.headers['X-Storlet-Generated-From-Account'] = quote(acct)
        resp.headers['X-Storlet-Generated-From'] = quote(path)
        if 'last-modified' in source_resp.headers:
            resp.headers['X-Storlet-Generated-From-Last-Modified'] = \
                source_resp.headers['last-modified']
        return resp

    def PUT(self):
        """
        PUT handler on Proxy
        """
        self.gateway.authorizeStorletExecution(self.request)
        self.gateway.augmentStorletRequest(self.request)
        if self.is_put_copy_request:
            self. _validate_copy_request()
            src_container, src_obj = check_copy_from_header(self.request)
            dest_container = self.container
            dest_object = self.obj
            self.request.headers.pop('X-Copy-From', None)
            return self.base_handle_copy_request(src_container, src_obj,
                                                 dest_container, dest_object)

        (out_md, app_iter) = \
            self.gateway.gatewayProxyPutFlow(self.request,
                                             self.container, self.obj)
        return self.handle_put_copy_response(out_md, app_iter)

    def COPY(self):
        """
        COPY handler on Proxy
        """
        if not self.request.headers.get('Destination'):
            return HTTPPreconditionFailed(request=self.request,
                                          body='Destination header required')

        self.gateway.authorizeStorletExecution(self.request)
        self.gateway.augmentStorletRequest(self.request)
        self._validate_copy_request()
        dest_container, dest_object = check_destination_header(self.request)

        # re-write the existing request as a PUT instead of creating a new one
        # TODO(eranr): do we want a new sub_request or re-write existing one as
        # we do below. See proxy obj controller COPY.
        self.request.method = 'PUT'
        self.request.path_info = '/v1/%s/%s/%s' % \
                                 (self.account, dest_container, dest_object)
        self.request.headers['Content-Length'] = 0
        del self.request.headers['Destination']

        return self.base_handle_copy_request(self.container, self.obj,
                                             dest_container, dest_object)


class StorletObjectHandler(BaseStorletHandler):
    def __init__(self, request, conf, app, logger):
        super(StorletObjectHandler, self).__init__(
            request, conf, app, logger)
        # object need the gateway module only execution
        if (self.is_storlet_execution):
            self._setup_gateway()
        else:
            raise NotStorletRequest()

    def _parse_vaco(self):
        _, _, acc, cont, obj = self.request.split_path(
            5, 5, rest_with_last=True)
        # TODO(kota_): make sure why object server api version is 0?
        return ('0', acc, cont, obj)

    @property
    def is_slo_get_request(self):
        """
        Determines from a GET request and its  associated response
        if the object is a SLO
        """
        return self.request.params.get('multipart-manifest') == 'get'

    def handle_request(self):
        if hasattr(self, self.request.method):
            return getattr(self, self.request.method)()
        else:
            # un-defined method should be NOT ALLOWED
            raise HTTPMethodNotAllowed(request=self.request)

    def _call_gateway(self, resp):
        return self.gateway.gatewayObjectGetFlow(
            self.request, self.container, self.obj, resp)

    def GET(self):
        self.logger.debug('GET. Run storlet')
        orig_resp = self.request.get_response(self.app)

        if not is_success(orig_resp.status_int):
            return orig_resp

        # TODO(takashi): not sure manifest file should not be run with storlet
        not_runnable = any(
            [self.is_range_request, self.is_slo_get_request,
             self.conf['storlet_execute_on_proxy_only'],
             self.is_slo_response(orig_resp)])

        if not_runnable:
            # No need to invoke Storlet in the object server
            # This is either an SLO or we are in proxy only mode
            self.logger.debug('storlet_handler: invocation '
                              'over %s/%s/%s %s' %
                              (self.account, self.container, self.obj,
                               'to be executed on proxy'))
            return orig_resp
        else:
            # We apply here the Storlet:
            self.logger.debug('storlet_handler: invocation '
                              'over %s/%s/%s %s' %
                              (self.account, self.container, self.obj,
                               'to be executed locally'))
            return self.apply_storlet(orig_resp)


class StorletHandlerMiddleware(object):

    def __init__(self, app, conf, storlet_conf):
        self.app = app
        self.logger = get_logger(conf, log_route='storlet_handler')
        self.stimeout = int(storlet_conf.get('storlet_timeout'))
        self.storlet_containers = [storlet_conf.get('storlet_container'),
                                   storlet_conf.get('storlet_dependency')]
        self.exec_server = storlet_conf.get('execution_server')
        self.handler_class = self._get_handler(self.exec_server)
        self.gateway_module = storlet_conf['gateway_module']
        self.proxy_only_storlet_execution = \
            storlet_conf['storlet_execute_on_proxy_only']
        self.gateway_conf = storlet_conf

    def _get_handler(self, exec_server):
        if exec_server == 'proxy':
            return StorletProxyHandler
        elif exec_server == 'object':
            return StorletObjectHandler
        else:
            raise ValueError(
                'configuration error: execution_server must be either proxy'
                ' or object but is %s' % exec_server)

    @wsgify
    def __call__(self, req):
        try:
            request_handler = self.handler_class(
                req, self.gateway_conf, self.app, self.logger)
            self.logger.debug('storlet_handler call in %s: with %s/%s/%s' %
                              (self.exec_server, request_handler.account,
                               request_handler.container, request_handler.obj))
        except HTTPException:
            raise
        except NotStorletRequest:
            return req.get_response(self.app)

        try:
            return request_handler.handle_request()

        except (StorletTimeout, ConnectionTimeout, Timeout):
            self.logger.exception('Storlet execution timed out')
            raise HTTPInternalServerError(body='Storlet execution timed out')
        except HTTPException:
            # TODO(takashi): Shoud we generate this log for all error?
            #                (ex. 404 when the object is not found)
            self.logger.exception('Storlet execution failed')
            raise
        except Exception:
            self.logger.exception('Storlet execution failed')
            raise HTTPInternalServerError(body='Storlet execution failed')


def filter_factory(global_conf, **local_conf):

    conf = global_conf.copy()
    conf.update(local_conf)
    storlet_conf = dict()
    storlet_conf['storlet_timeout'] = conf.get('storlet_timeout', 40)
    storlet_conf['storlet_container'] = \
        conf.get('storlet_container', 'storlet')
    storlet_conf['storlet_dependency'] = conf.get('storlet_dependency',
                                                  'dependency')
    storlet_conf['execution_server'] = conf.get('execution_server', '')
    storlet_conf['storlet_execute_on_proxy_only'] = \
        config_true_value(conf.get('storlet_execute_on_proxy_only', 'false'))
    storlet_conf['gateway_conf'] = {}
    storlet_conf['reseller_prefix'] = conf.get('reseller_prefix', 'AUTH')

    module_name = conf.get('storlet_gateway_module', '')
    mo = module_name[:module_name.rfind(':')]
    cl = module_name[module_name.rfind(':') + 1:]
    module = __import__(mo, fromlist=[cl])
    the_class = getattr(module, cl)

    configParser = ConfigParser.RawConfigParser()
    configParser.read(conf.get('storlet_gateway_conf',
                               '/etc/swift/storlet_stub_gateway.conf'))

    additional_items = configParser.items("DEFAULT")
    for key, val in additional_items:
        storlet_conf[key] = val

    swift_info = {}
    storlet_conf["gateway_module"] = the_class
    register_swift_info('storlet_handler', False, **swift_info)

    def storlet_handler_filter(app):
        return StorletHandlerMiddleware(app, conf, storlet_conf)
    return storlet_handler_filter
