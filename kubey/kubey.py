import os
import re
import logging
import jmespath

from .kubectl import KubeCtl
from .cache import Cache
from .pod import Pod
from .container import Container
from .node_condition import NodeCondition


_logger = logging.getLogger(__name__)


class Kubey(object):
    class UnknownNamespace(ValueError):
        pass

    ANY = '.'

    POD_COLUMN_MAP = {
        'name': 'metadata.name',
        'namespace': 'metadata.namespace',
        'node_name': 'spec.nodeName',
        'host_ip': 'status.hostIP',
        'phase': 'status.phase',
        # 'conditions': 'status.conditions[*].[type,status,message]',
        'containers': 'status.containerStatuses[*].[name,ready,state,image]',
    }

    NODE_COLUMN_MAP = {
        'name': 'metadata.name',
        'addresses': 'status.addresses[*].address',
        'conditions': 'status.conditions[*].[type,status,message]',
    }

    def __init__(self, config):
        self._config = config
        self.kubectl = KubeCtl(config.context)
        self._split_match()
        self._namespaces = self._cache('namespaces')
        self._nodes_cache = self._cache('nodes')
        self._pods_cache = self._cache('pods', '--all-namespaces')
        self._set_namespace()

    def __repr__(self):
        return "<Kubey: context=%s namespace=%s match=%s/%s/%s>" % (
            self.kubectl.context, self._config.namespace,
            self._node_re.pattern, self._pod_re.pattern, self._container_re.pattern)

    def each_pod(self):
        matched = 0
        for info in self._pods_cache.obj()['items']:
            md = info['metadata']
            if (self._namespace_re.search(md['namespace']) and
                self._node_re.search(info['spec'].get('nodeName', '.')) and
                self._pod_re.search(md['name'])):
                pod = Pod(self._config, info, self._container_re.search)
                yield(pod)
                matched += 1
                if self._config.maximum and self._config.maximum <= matched:
                    _logger.debug('Prematurely stopping at match maximum of ' +
                                  str(self._config.maximum))
                    break

    def each_node(self, *columns):
        columns = self._list_from(columns)
        pod_index = self._index_of('pods', columns)
        if pod_index:
            columns = list(columns)
            del(columns[pod_index])
        condition_index = self._index_of('conditions', columns)
        cols = ['name', 'conditions'] + columns
        query = 'items[?kind==\'Node\'].[' + \
                ','.join([self.NODE_COLUMN_MAP[c] for c in cols]) + ']'
        count = 0
        for node in jmespath.search(query, self._nodes_cache.obj()):
            (name, condition_info), col_values = node[:2], node[2:]  # FIXME: duplication
            if not self._node_re.search(name):
                continue
            count += 1
            if self._config.maximum and self._config.maximum < count:
                _logger.debug('Prematurely stopping at match maximum of ' +
                              str(self._config.maximum))
                break
            if condition_index:
                col_values[condition_index] = [
                    NodeCondition(self._config, *c) for c in condition_info
                ]
            if pod_index:
                pods = []
                for namespace, node_name, pod_name in self.each_pod('namespace', 'node', 'name'):
                    if not (node_name == name and
                            self._namespace_re.search(namespace) and
                            self._pod_re.search(pod_name)):
                        continue
                    if self._config.namespace == self.ANY:
                        pod_name = namespace + '/' + pod_name
                    pods.append(pod_name)
                if not pods:
                    continue  # no matching pods, skip this node
                col_values.insert(pod_index, pods)
            yield(col_values)

    # Private:

    @staticmethod
    def _list_from(lst):
        if lst and len(lst) == 1 and (isinstance(lst, list) or isinstance(lst, tuple)):
            return lst[0]
        return lst

    @staticmethod
    def _index_of(name, columns):
        return columns.index(name) if name in columns else None

    def _set_namespace(self):
        self._namespace_re = re.compile(self._config.namespace)
        if self._config.namespace == self.ANY:
            self._namespace_query = 'items[*]'
        else:
            self._namespace_query = 'items[?contains(metadata.namespace,\'%s\')]' % (
                self._config.namespace)
        if (self._config.namespace == self.ANY):
            return
        validation_query = 'items[?contains(metadata.name,\'%s\')].status.phase' % (
            self._config.namespace)
        if not jmespath.search(validation_query, self._namespaces.obj()):
            raise self.UnknownNamespace(self._config.namespace)

    def _split_match(self):
        match_items = self._config.match.split('/', 2)
        node = match_items.pop(0) if len(match_items) > 2 else ''
        pod = match_items.pop(0)
        container = match_items.pop(0) if len(match_items) > 0 else ''
        self._node_re = re.compile(node, re.IGNORECASE)
        self._pod_re = re.compile(pod, re.IGNORECASE)
        self._container_re = re.compile(container, re.IGNORECASE)

    def _cache(self, name, *args):
        cache_fn = os.path.join(
            self._config.cache_path, '.%s_%s_%s' % (__name__, self.kubectl.context, name)
        )
        return Cache(
            cache_fn, self._config.cache_seconds, self.kubectl.call_json, 'get', name, *args
        )
