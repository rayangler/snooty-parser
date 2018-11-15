import collections
import docutils.nodes
import logging
import multiprocessing
import os
import pwd
import subprocess
from functools import partial
from typing import Any, Dict, Optional, Set, List
from typing_extensions import Protocol
import docutils.utils

from . import gizaparser, rstparser, util
from .gizaparser.nodes import GizaRegistry
from .types import Diagnostic, SerializableType, EmbeddedRstParser, Page, StaticAsset

RST_EXTENSIONS = {'.rst', '.txt'}
logger = logging.getLogger(__name__)


class JSONVisitor(rstparser.Visitor):
    """Node visitor that creates a JSON-serializable structure."""
    def __init__(self, project_root: str, docpath: str, document: docutils.nodes.document) -> None:
        self.project_root = project_root
        self.docpath = docpath
        self.document = document
        self.state: List[Dict[str, Any]] = []
        self.diagnostics: List[Diagnostic] = []
        self.static_assets: Set[StaticAsset] = set()

    def dispatch_visit(self, node: docutils.nodes.Node) -> None:
        node_name = node.__class__.__name__
        if node_name == 'system_message':
            msg = node[0].astext()
            self.diagnostics.append(Diagnostic.warning(msg, util.get_line(node)))
            raise docutils.nodes.SkipNode()
        elif node_name in ('definition', 'field_list'):
            return

        if node_name == 'document':
            self.state.append({
                'type': 'root',
                'children': [],
                'position': {
                    'start': {'line': 0}
                }
            })
            return

        doc: Dict[str, SerializableType] = {
            'type': node_name,
            'position': {
                'start': {'line': util.get_line(node)}
            }
        }

        if node_name == 'field':
            key = node.children[0].astext()
            value = node.children[1].astext()
            self.state[-1].setdefault('options', {})[key] = value
            raise docutils.nodes.SkipNode()

        self.state.append(doc)

        if node_name == 'Text':
            doc['type'] = 'text'
            doc['value'] = str(node)
            return

        # Most, but not all, nodes have children nodes
        make_children = True

        if node_name == 'directive':
            self.handle_directive(node, doc)
        elif node_name == 'role':
            doc['name'] = node['name']
            doc['label'] = node['label']
            doc['target'] = node['target']
            doc['raw'] = node['raw']
        elif node_name == 'target':
            doc['type'] = 'target'
            doc['ids'] = node['ids']
        elif node_name == 'definition_list':
            doc['type'] = 'definitionList'
        elif node_name == 'definition_list_item':
            doc['type'] = 'definitionListItem'
            doc['term'] = []
        elif node_name == 'bullet_list':
            doc['type'] = 'list'
            doc['ordered'] = False
        elif node_name == 'enumerated_list':
            doc['type'] = 'list'
            doc['ordered'] = True
        elif node_name == 'list_item':
            doc['type'] = 'listItem'
        elif node_name == 'title':
            doc['type'] = 'heading'
        elif node_name == 'FixedTextElement':
            doc['type'] = 'literal'
            doc['value'] = node.astext()
            make_children = False

        if make_children:
            doc['children'] = []

    def dispatch_departure(self, node: docutils.nodes.Node) -> None:
        node_name = node.__class__.__name__
        if len(self.state) == 1 or node_name == 'definition':
            return

        popped = self.state.pop()

        if popped['type'] == 'term':
            self.state[-1]['term'] = popped['children']
        else:
            if 'children' not in self.state[-1]:
                print(self.state[-1])
            self.state[-1]['children'].append(popped)

    def handle_directive(self, node: docutils.nodes.Node, doc: Dict[str, SerializableType]) -> None:
        name = node['name']
        doc['name'] = name

        if node.children and node.children[0].__class__.__name__ == 'directive_argument':
            visitor = JSONVisitor(self.project_root, self.docpath, self.document)
            node.children[0].walkabout(visitor)
            argument = visitor.state[-1]['children']
            doc['argument'] = argument
            options = node['options']
            doc['options'] = options
            node.children = node.children[1:]
        else:
            argument = []
            options = {}
            doc['argument'] = argument

        argument_text = None
        try:
            argument_text = argument[0]['value']
        except (IndexError, KeyError):
            pass

        if name == 'figure':
            if argument_text is None:
                self.diagnostics.append(
                    Diagnostic.error('"figure" expected a path argument', util.get_line(node)))
                return

            try:
                static_asset = self.add_static_asset(argument_text)
                options['checksum'] = static_asset.checksum
            except OSError as err:
                msg = '"figure" could not open "{}": {}'.format(
                    argument_text, os.strerror(err.errno))
                self.diagnostics.append(Diagnostic.error(msg, util.get_line(node)))

    def add_static_asset(self, path: str) -> StaticAsset:
        fileid, path = util.reroot_path(path, self.docpath, self.project_root)
        static_asset = StaticAsset.load(fileid, path)
        self.static_assets.add(static_asset)
        return static_asset


class InlineJSONVisitor(JSONVisitor):
    """A JSONVisitor subclass which does not emit block nodes."""
    def dispatch_visit(self, node: docutils.nodes.Node) -> None:
        if isinstance(node, docutils.nodes.Body):
            return

        JSONVisitor.dispatch_visit(self, node)

    def dispatch_departure(self, node: docutils.nodes.Node) -> None:
        if isinstance(node, docutils.nodes.Body):
            return

        JSONVisitor.dispatch_departure(self, node)


def parse_rst(parser: rstparser.Parser[JSONVisitor], path: str, text: Optional[str] = None) -> Page:
    if text is None:
        with open(path, 'r') as f:
            text = f.read()
    visitor = parser.parse(path, text)

    return Page(
        path,
        text,
        visitor.state[-1],
        visitor.diagnostics,
        visitor.static_assets)


def make_embedded_rst_parser(project_root: str, page: Page) -> EmbeddedRstParser:
    def parse_embedded_rst(rst: str,
                           lineno: int,
                           inline: bool) -> List[SerializableType]:
        # Crudely make docutils line numbers match
        text = '\n' * lineno + rst.strip()
        visitor_class = InlineJSONVisitor if inline else JSONVisitor
        parser = rstparser.Parser(project_root, visitor_class)
        visitor = parser.parse(page.path, text)
        children: List[SerializableType] = visitor.state[-1]['children']

        page.diagnostics.extend(visitor.diagnostics)
        page.static_assets.update(visitor.static_assets)

        return children

    return parse_embedded_rst


def get_giza_category(path: str) -> str:
    return os.path.basename(path).split('-', 1)[0]


class ProjectBackend(Protocol):
    def on_progress(self, progress: int, total: int, message: str) -> None: ...

    def on_update(self, prefix: List[str], page_id: str, page: Page) -> None: ...

    def on_delete(self, page_id: str) -> None: ...


class Project:
    def __init__(self,
                 name: str,
                 root: str,
                 backend: ProjectBackend) -> None:
        self.root = root
        self.parser = rstparser.Parser(self.root, JSONVisitor)
        self.static_assets: Dict[str, Set[StaticAsset]] = collections.defaultdict(set)
        self.backend = backend

        self.steps_registry: GizaRegistry[gizaparser.steps.Step] = GizaRegistry()
        self.yaml_mapping = {
            'steps': gizaparser.steps.GizaStepsCategory(self.steps_registry)
        }

        username = pwd.getpwuid(os.getuid()).pw_name
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            encoding='utf-8').strip()
        self.prefix = [name, username, branch]

    def get_page_id(self, path: str) -> str:
        path = os.path.normpath(path)
        return '/'.join(self.prefix + [path.split('.')[0].split('/', 1)[1]])

    def update(self, path: str, optional_text: Optional[str] = None) -> None:
        prefix = get_giza_category(path)
        _, ext = os.path.splitext(path)
        page: Optional[Page] = None
        if ext in RST_EXTENSIONS:
            page = parse_rst(self.parser, path, optional_text)
        elif ext == '.yaml' and prefix in self.yaml_mapping:
            file_id = os.path.basename(path)
            giza_category = self.yaml_mapping[prefix]
            needs_rebuild = self.steps_registry.dg.dependents[file_id].union(set([file_id]))
            logger.debug('needs_rebuild: %s', ','.join(needs_rebuild))
            for file_id in needs_rebuild:
                try:
                    giza_node = giza_category.registry.reify_file_id(file_id)
                except KeyError:
                    logging.warn('No file found in registry: %s', file_id)
                    continue

                steps, text, diagnostics = giza_category.parse(path, optional_text)
                page = Page(giza_node.path, text, {}, diagnostics, set())
                giza_category.registry.add(path, text, steps)
                embedded_parser = make_embedded_rst_parser(self.root, page)
                giza_category.to_page(page, giza_node.data, embedded_parser)
                path = page.path
        else:
            raise ValueError('Unknown file type: ' + path)

        if page:
            self.backend.on_update(self.prefix, self.get_page_id(path), page)

    def delete(self, path: str) -> None:
        file_id = os.path.basename(path)
        for giza_category in self.yaml_mapping.values():
            del giza_category.registry[file_id]

        self.backend.on_delete(self.get_page_id(path))

    def build(self) -> None:
        all_yaml_diagnostics: Dict[str, List[Diagnostic]] = {}
        with multiprocessing.Pool() as pool:
            paths = util.get_files(self.root, RST_EXTENSIONS)
            logger.debug('Processing rst files')
            for page in pool.imap_unordered(partial(parse_rst, self.parser), paths):
                self.backend.on_update(self.prefix, self.get_page_id(page.path), page)

            # Categorize our YAML files
            logger.debug('Categorizing YAML files')
            categorized: Dict[str, List[str]] = collections.defaultdict(list)
            for path in util.get_files(self.root, ('.yaml',)):
                prefix = get_giza_category(path)
                if prefix in self.yaml_mapping:
                    categorized[prefix].append(path)

            # Initialize our YAML file registry
            for prefix, giza_category in self.yaml_mapping.items():
                logger.debug('Parsing %s YAML', prefix)
                paths_in_category: List[str] = categorized[prefix]
                for path, (steps, text, diagnostics) in zip(
                        paths_in_category,
                        pool.imap_unordered(giza_category.parse, paths_in_category)):
                    all_yaml_diagnostics[path] = diagnostics
                    giza_category.registry.add(path, text, steps)

        # Now that all of our YAML files are loaded, generate a page for each one
        for prefix, giza_category in self.yaml_mapping.items():
            logger.debug('Processing %s YAML: %d nodes', prefix, len(giza_category.registry))
            for file_id, giza_node in giza_category.registry:
                page = Page(giza_node.path, giza_node.text, {}, [], set())
                embedded_parser = make_embedded_rst_parser(self.root, page)
                giza_category.to_page(page, giza_node.data, embedded_parser)
                page.diagnostics.extend(all_yaml_diagnostics.get(giza_node.path, []))
                self.backend.on_update(self.prefix, self.get_page_id(page.path), page)
