__author__ = "Giacomo Bergami, Oliver Robert Fox"
__copyright__ = "Copyright 2025"
__license__ = "GPLv3"
__version__ = "3.0"

"""
pyoxigraph-based replacement for FunctionalMatch.rdf.RDFGraph.
Provides the same public API as RDFGraph but uses pyoxigraph for in-memory
RDF graph storage and SPARQL querying, which is significantly faster than
rdflib for large ontologies.
"""

import io
import re
import urllib.parse

import pyoxigraph

_RDF_NS  = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL_NS  = "http://www.w3.org/2002/07/owl#"
_XSD_NS  = "http://www.w3.org/2001/XMLSchema#"
_FOAF_NS = "http://xmlns.com/foaf/0.1/"

# Frequently-used pyoxigraph nodes
_RDF_TYPE      = pyoxigraph.NamedNode(_RDF_NS  + "type")
_RDFS_LABEL    = pyoxigraph.NamedNode(_RDFS_NS + "label")
_RDFS_SUBCLSOF = pyoxigraph.NamedNode(_RDFS_NS + "subClassOf")
_RDFS_COMMENT  = pyoxigraph.NamedNode(_RDFS_NS + "comment")
_OWL_CLASS     = pyoxigraph.NamedNode(_OWL_NS  + "Class")
_OWL_OBJPROP   = pyoxigraph.NamedNode(_OWL_NS  + "ObjectProperty")

_DEFAULT_GRAPH = pyoxigraph.DefaultGraph()

# Standard namespace prefixes automatically prepended to every SPARQL query
_STANDARD_PREFIXES = {
    "rdf":  _RDF_NS,
    "rdfs": _RDFS_NS,
    "owl":  _OWL_NS,
    "xsd":  _XSD_NS,
    "foaf": _FOAF_NS,
}


class Namespace:
    """Mimics rdflib.Namespace – subscript returns a pyoxigraph.NamedNode."""

    def __init__(self, uri: str):
        self._uri = str(uri)

    def __getitem__(self, name: str) -> pyoxigraph.NamedNode:
        return pyoxigraph.NamedNode(self._uri + name)

    def __str__(self) -> str:
        return self._uri

    def __len__(self) -> int:
        return len(self._uri)

    def __eq__(self, other) -> bool:
        return self._uri == str(other)

    def __hash__(self) -> int:
        return hash(self._uri)

    def __repr__(self) -> str:
        return f"Namespace({self._uri!r})"


class _NSHelper:
    """Attribute-access namespace helper (mimics rdflib namespace objects)."""

    def __init__(self, uri: str):
        self._uri = uri

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return pyoxigraph.NamedNode(self._uri + name)

    def __getitem__(self, name: str):
        return pyoxigraph.NamedNode(self._uri + name)


class _XSDHelper(_NSHelper):
    """XSD namespace – XSD.string etc. return pyoxigraph.NamedNode."""
    pass


XSD  = _XSDHelper(_XSD_NS)
RDF  = _NSHelper(_RDF_NS)
RDFS = _NSHelper(_RDFS_NS)
OWL  = _NSHelper(_OWL_NS)
FOAF = _NSHelper(_FOAF_NS)


def BNode():
    """Return a fresh pyoxigraph.BlankNode (mimics rdflib.BNode())."""
    return pyoxigraph.BlankNode()


def URIRef(uri: str) -> pyoxigraph.NamedNode:
    """Return a pyoxigraph.NamedNode (mimics rdflib.URIRef)."""
    return pyoxigraph.NamedNode(str(uri))


class _TermWrapper:
    """
    Wraps a pyoxigraph term (NamedNode, BlankNode) to provide the .value
    property and string conversion expected by rdflib-style callers.
    """

    __slots__ = ("_ox",)

    def __init__(self, ox_term):
        self._ox = ox_term

    @property
    def value(self):
        return self._ox.value

    def __str__(self) -> str:
        return self._ox.value

    def __repr__(self) -> str:
        return f"_TermWrapper({self._ox!r})"

    def __eq__(self, other) -> bool:
        return str(self) == str(other)

    def __lt__(self, other) -> bool:
        v = other.value if isinstance(other, _TermWrapper) else other
        return self._ox.value < str(v)

    def __le__(self, other) -> bool:
        return self == other or self < other

    def __gt__(self, other) -> bool:
        return not self <= other

    def __ge__(self, other) -> bool:
        return not self < other

    def __hash__(self) -> int:
        return hash(str(self))

    def __bool__(self) -> bool:
        return self._ox is not None

    def to_pyoxigraph(self):
        return self._ox


class Literal(_TermWrapper):
    """
    Compat Literal class.

    Can be constructed like rdflib.Literal(value, datatype=...) to create a
    new literal for use as a SPARQL binding, OR from an existing
    pyoxigraph.Literal to wrap a query-result term.

    ``isinstance(x, Literal)`` returns True only for literal terms.
    """

    def __init__(self, value_or_ox=None, datatype=None, lang=None):
        if isinstance(value_or_ox, pyoxigraph.Literal):
            # Wrapping an existing pyoxigraph Literal from a query result
            super().__init__(value_or_ox)
        elif isinstance(value_or_ox, _TermWrapper):
            # Re-wrapping from another wrapper
            super().__init__(value_or_ox._ox)
        else:
            # Building a new Literal from a Python value
            v = str(value_or_ox) if value_or_ox is not None else ""
            if datatype is not None:
                if isinstance(datatype, pyoxigraph.NamedNode):
                    dt = datatype
                else:
                    dt = pyoxigraph.NamedNode(str(datatype))
                super().__init__(pyoxigraph.Literal(v, datatype=dt))
            elif lang is not None:
                super().__init__(pyoxigraph.Literal(v, language=lang))
            else:
                super().__init__(pyoxigraph.Literal(v))

    @property
    def value(self):
        """Return a Python-typed value (bool/int/float/str)."""
        ox = self._ox
        dt = ox.datatype.value if ox.datatype else None
        v = ox.value
        if dt == _XSD_NS + "boolean":
            return v == "true"
        if dt == _XSD_NS + "integer":
            return int(v)
        if dt in (_XSD_NS + "double", _XSD_NS + "float", _XSD_NS + "decimal"):
            return float(v)
        return v

    @property
    def datatype(self):
        return self._ox.datatype  # pyoxigraph.NamedNode or None


class _QueryRow:
    """
    Wraps a pyoxigraph.QuerySolution to provide rdflib-style attribute access
    and an asdict() method.
    """

    def __init__(self, solution, variables):
        # Use __dict__ directly to avoid triggering __getattr__
        self.__dict__["_solution"] = solution
        self.__dict__["_vars"] = variables  # list[str]

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        sol = self.__dict__["_solution"]
        vars_ = self.__dict__["_vars"]
        if name not in vars_:
            return None
        term = sol[name]
        return _wrap_term(term)

    def asdict(self) -> dict:
        sol = self.__dict__["_solution"]
        vars_ = self.__dict__["_vars"]
        return {v: _wrap_term(sol[v]) for v in vars_}

    def get(self, key, default=None):
        sol = self.__dict__["_solution"]
        vars_ = self.__dict__["_vars"]
        if key not in vars_:
            return default
        term = sol[key]
        return _wrap_term(term) if term is not None else default


def _wrap_term(term):
    """Wrap a pyoxigraph term in the appropriate compat class."""
    if term is None:
        return None
    if isinstance(term, pyoxigraph.Literal):
        return Literal(term)
    return _TermWrapper(term)


def _to_sparql_val(term) -> str:
    """
    Convert a binding value to a SPARQL term string suitable for a VALUES
    clause.  Returns "UNDEF" for unsupported term types.
    """
    ox = None
    if isinstance(term, _TermWrapper):
        ox = term._ox
    elif isinstance(term, pyoxigraph.NamedNode):
        ox = term
    elif isinstance(term, pyoxigraph.Literal):
        ox = term
    elif isinstance(term, pyoxigraph.BlankNode):
        return "UNDEF"
    elif isinstance(term, str):
        ox = pyoxigraph.NamedNode(term)
    else:
        return "UNDEF"

    if isinstance(ox, pyoxigraph.NamedNode):
        return f"<{ox.value}>"
    if isinstance(ox, pyoxigraph.Literal):
        v = (ox.value
             .replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t"))
        if ox.datatype:
            return f'"{v}"^^<{ox.datatype.value}>'
        if ox.language:
            return f'"{v}"@{ox.language}'
        return f'"{v}"'
    return "UNDEF"


def _inject_bindings(query: str, bindings: dict) -> str:
    """
    Inject a SPARQL VALUES clause for each binding into the WHERE block.
    """
    if not bindings:
        return query
    parts = []
    for var, term in bindings.items():
        val = _to_sparql_val(term)
        if val != "UNDEF":
            parts.append(f"VALUES (?{var}) {{ ({val}) }}")
    if not parts:
        return query
    values_str = " ".join(parts)
    return re.sub(
        r"WHERE\s*\{",
        f"WHERE {{ {values_str}",
        query, count=1, flags=re.IGNORECASE,
    )


def _prepend_prefixes(query: str, prefixes: dict) -> str:
    """Prepend PREFIX declarations to a SPARQL query string."""
    lines = [f"PREFIX {p}: <{u}>" for p, u in prefixes.items()]
    return "\n".join(lines) + "\n" + query


def _lit(s: str) -> pyoxigraph.Literal:
    return pyoxigraph.Literal(str(s), datatype=pyoxigraph.NamedNode(_XSD_NS + "string"))

def _boolean(s: bool) -> pyoxigraph.Literal:
    return pyoxigraph.Literal("true" if s else "false",
                               datatype=pyoxigraph.NamedNode(_XSD_NS + "boolean"))

def _integer(s: int) -> pyoxigraph.Literal:
    return pyoxigraph.Literal(str(int(s)),
                               datatype=pyoxigraph.NamedNode(_XSD_NS + "integer"))

def _double(s: float) -> pyoxigraph.Literal:
    return pyoxigraph.Literal(str(s),
                               datatype=pyoxigraph.NamedNode(_XSD_NS + "double"))

def _val_to_ox(val):
    """Convert a Python value to an appropriate pyoxigraph Literal."""
    if isinstance(val, bool):
        return _boolean(val)
    if isinstance(val, int):
        return _integer(val)
    if isinstance(val, float):
        return _double(val)
    return _lit(str(val))

def onta(ns, s: str) -> pyoxigraph.NamedNode:
    return pyoxigraph.NamedNode(ns._uri + urllib.parse.quote_plus(s))

def _add(store, s, p, o):
    store.add(pyoxigraph.Quad(s, p, o, _DEFAULT_GRAPH))


class RDFGraph:
    """
    Drop-in replacement for FunctionalMatch.rdf.RDFGraph using pyoxigraph
    instead of rdflib for in-memory graph operations.

    Only the ``databaseConn=False`` (in-memory) path is implemented.
    Passing ``databaseConn=True`` raises NotImplementedError because
    pyoxigraph does not support a PostgreSQL/SQLAlchemy backend.
    """

    def __init__(self, name, namespace, user, password, hostame, port, database,
                 databaseConn=True):
        self.databaseConn = databaseConn
        self.database = database
        self.port = port
        self.hostame = hostame
        self.password = password
        self.user = user
        self.namespace = Namespace(namespace)
        assert isinstance(name, str)
        self.name = name
        self.graph = None
        self.uri = (
            f"postgresql+psycopg2://{user}:{password}@{hostame}:{port}/{database}"
        )
        self._started = False
        self._stopped = True
        self.names = None
        self.classes = None
        self.relationships = None
        self._ns_prefixes = dict(_STANDARD_PREFIXES)

    def start(self):
        if self._started:
            return False
        if not self._actual_start():
            return False
        self._started = True
        self._stopped = False
        return True

    def stop(self):
        if self._stopped or not self._started:
            self._stopped = True
            return True
        if not self._actual_stop():
            return False
        self._started = False
        self._stopped = True
        return True

    def clear(self):
        if self._started and self.graph is not None:
            self.graph.clear()
            return True
        return False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()

    def hasDBStoredData(self):
        if not self.databaseConn:
            return True
        # PostgreSQL path – not needed in practice; kept for API compatibility
        try:
            from sqlalchemy import create_engine
            from sqlalchemy_utils import database_exists
            engine = create_engine(
                "postgresql://{}:{}@{}:{}/{}".format(
                    self.user, self.password, self.hostame, self.port, self.database
                )
            )
            return database_exists(engine.url)
        except Exception:
            return False

    def _actual_start(self):
        if self.databaseConn:
            raise NotImplementedError(
                "PostgreSQL/SQLAlchemy storage is not supported by the "
                "pyoxigraph-backed RDFGraph. Use databaseConn=False."
            )
        # Use a persistent RocksDB-backed store when a path is provided so the
        # parsed graph survives across process restarts.
        # Worker processes share the same store path as the main process but
        # only need read access; fall back to read-only if the write lock is
        # already held (OSError: LOCK: Resource temporarily unavailable).
        store_path = getattr(self, '_store_path', None)
        self.graph = pyoxigraph.Store(store_path) if store_path else pyoxigraph.Store()
        self.names = {}
        self.relationships = {}
        self.classes = {}
        # Register the graph's own namespace prefix
        self._ns_prefixes[self.name] = str(self.namespace)
        return True

    def _actual_stop(self):
        self.names = None
        self.classes = None
        self.relationships = None
        self.graph = None  # explicitly release the pyoxigraph Store and its RocksDB lock
        return True

    def parse(self, file):
        """Load RDF data from a file path (str) or binary file-like object."""
        if isinstance(file, str):
            fmt = self._detect_format(file)
            with open(file, "rb") as fh:
                self.graph.load(fh, format=fmt)
        else:
            data = file.read() if hasattr(file, "read") else file
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.graph.load(io.BytesIO(data), format=pyoxigraph.RdfFormat.TURTLE)

    @staticmethod
    def _detect_format(filename: str):
        lo = filename.lower()
        if lo.endswith(".nt"):
            return pyoxigraph.RdfFormat.N_TRIPLES
        if lo.endswith(".n3"):
            return pyoxigraph.RdfFormat.N3
        if lo.endswith(".xml") or lo.endswith(".rdf"):
            return pyoxigraph.RdfFormat.RDF_XML
        if lo.endswith(".trig"):
            return pyoxigraph.RdfFormat.TRIG
        if lo.endswith(".nq"):
            return pyoxigraph.RdfFormat.N_QUADS
        return pyoxigraph.RdfFormat.TURTLE

    def serialize(self, filename):
        """Serialize the graph to an N-Quads file."""
        with open(filename, "wb") as fh:
            self.graph.dump(fh, format=pyoxigraph.RdfFormat.N_QUADS)

    def create_property(self, name, comment=None):
        if name not in self.relationships:
            node = pyoxigraph.NamedNode(self.namespace._uri + name)
            _add(self.graph, node, _RDF_TYPE, _OWL_OBJPROP)
            self.relationships[name] = node
            if comment is not None:
                _add(self.graph, node, _RDFS_COMMENT, _lit(comment))
        return self.relationships[name]

    def create_relationship(self, name, comment=None):
        if name not in self.relationships:
            self.relationships[name] = pyoxigraph.NamedNode(
                self.namespace._uri + name
            )
            if comment is not None:
                _add(
                    self.graph,
                    self.relationships[name],
                    _RDFS_COMMENT,
                    _lit(comment),
                )
        return self.relationships[name]

    def create_relationship_instance(self, src, rel, dst, refl=False):
        assert src in self.names
        assert dst in self.names
        rel = self.create_relationship(rel)
        _add(self.graph, self.names[src], rel, self.names[dst])
        if refl:
            _add(self.graph, self.names[dst], rel, self.names[src])

    def extract_properties(self, obj_src, kwargs):
        for k, val in kwargs.items():
            if val is None:
                continue
            if k not in self.relationships:
                rel_node = pyoxigraph.NamedNode(self.namespace._uri + k)
                _add(self.graph, rel_node, _RDF_TYPE, _OWL_OBJPROP)
                self.relationships[k] = rel_node
            rel = self.relationships[k]
            if isinstance(val, dict):
                bn = pyoxigraph.BlankNode()
                _add(self.graph, obj_src, rel, bn)
                self.extract_properties(bn, val)
            elif isinstance(val, (list, tuple)):
                for x in val:
                    _add(self.graph, obj_src, rel, _val_to_ox(x))
            else:
                _add(self.graph, obj_src, rel, _val_to_ox(val))

    def create_entity(self, name, clazzL=None, label=None, comment=None, **kwargs):
        if label is None:
            label = name
        if name not in self.names:
            self.names[name] = onta(self.namespace, name)
        node = self.names[name]
        if clazzL is not None:
            clazz_list = clazzL if isinstance(clazzL, list) else [clazzL]
            for clazz in clazz_list:
                assert clazz in self.classes
                _add(self.graph, node, _RDF_TYPE, self.classes[clazz])
            _add(self.graph, node, _RDFS_LABEL, _lit(label))
        self.extract_properties(node, kwargs)
        if comment is not None:
            _add(self.graph, node, _RDFS_COMMENT, _lit(comment))
        return node

    def create_class(self, name, subclazzOf=None, comment=None):
        if name not in self.classes:
            clazz = onta(self.namespace, name)
            _add(self.graph, clazz, _RDF_TYPE, _OWL_CLASS)
            if subclazzOf is not None:
                parents = subclazzOf if isinstance(subclazzOf, list) else [subclazzOf]
                for parent in parents:
                    if isinstance(parent, str):
                        parent = self.create_class(parent)
                    _add(self.graph, clazz, _RDFS_SUBCLSOF, parent)
            self.classes[name] = clazz
        if comment is not None:
            _add(self.graph, self.classes[name], _RDFS_COMMENT, _lit(comment))
        return self.classes[name]

    def create_concept(self, full_name, type, hasAdjective=None, entryPoint=None,
                       subject=None, d_object=None, entity_name=None,
                       composite_with=None, comment=None, **kwargs):
        if entity_name is None:
            entity_name = full_name
        ref = self.create_entity(full_name, type, label=entity_name)
        ep = ref if entryPoint is None else self.names[entryPoint]
        _add(self.graph, ref, self.relationships["entryPoint"], ep)
        from collections.abc import Iterable as _Iter
        if hasAdjective is not None and isinstance(hasAdjective, _Iter):
            assert hasAdjective in self.names
            _add(self.graph, ref, self.relationships["hasAdjective"],
                 self.names[hasAdjective])
        if d_object is not None:
            assert subject is not None
        if composite_with is not None:
            for comp in composite_with:
                assert comp in self.names
                _add(self.graph, ref,
                     self.relationships["composite_form_with"], self.names[comp])
        if subject is not None:
            assert subject in self.names
            _add(self.graph, ref, self.relationships["subject"], self.names[subject])
            if d_object is not None:
                _add(self.graph, ref, self.relationships["d_object"],
                     self.names[d_object])
        for k, val in kwargs.items():
            if k not in self.relationships:
                rn = pyoxigraph.NamedNode(self.namespace._uri + k)
                _add(self.graph, rn, _RDF_TYPE, _OWL_OBJPROP)
                self.relationships[k] = rn
            _add(self.graph, ref, self.relationships[k], _val_to_ox(val))
        if comment is not None:
            _add(self.graph, ref, _RDFS_COMMENT, _lit(comment))
        return ref

    # ------------------------------------------------------------------ #
    #  SPARQL helpers                                                      #
    # ------------------------------------------------------------------ #

    def _run_query(self, query: str, bindings: dict = None):
        """
        Execute a SPARQL SELECT query, prepending namespace prefixes and
        injecting any variable bindings as VALUES clauses.
        """
        q = _prepend_prefixes(query, self._ns_prefixes)
        if bindings:
            q = _inject_bindings(q, bindings)
        try:
            return self.graph.query(q)
        except Exception as exc:
            raise RuntimeError(
                f"SPARQL query failed:\n{q}\n\nError: {exc}"
            ) from exc

    def _iter_rows(self, query: str, bindings: dict = None):
        """Run a SPARQL query and yield _QueryRow objects."""
        results = self._run_query(query, bindings)
        vars_ = [v.value for v in results.variables]
        for solution in results:
            yield _QueryRow(solution, vars_)

    def _single_unary_query(self, knows_query: str, f=None):
        for row in self._iter_rows(knows_query):
            yield f(row) if f is not None else row

    def string_query(self, knows_query: str, attr=None):
        if attr is None:
            return self._single_unary_query(knows_query, str)
        return self._single_unary_query(knows_query, lambda x: str(getattr(x, attr)))

    def _run_custom_sparql_query(self, query: str, bindings: dict = None):
        for row in self._iter_rows(query, bindings):
            yield row.asdict()

    def single_edge_src_multipoint(self, src, src_spec, edge_type, dst):
        q = f"""
         SELECT DISTINCT ?src ?edge_type ?dst ?src_label ?src_spec ?dst_label
         WHERE {{
             ?src ?edge_type ?dst.
             ?src {self.name}:entryPoint ?src_entry.
             ?src_entry rdfs:label ?src_label.
             ?src {self.name}:hasAdjective ?src_spec_node.
             ?src_spec_node rdfs:label ?src_spec.
             ?dst rdfs:label ?dst_label .
         }}"""
        bindings = {}
        srcBool = srcSpecBool = edgeBool = dstBool = False
        if not src.startswith("^"):
            bindings["src_label"] = Literal(src, datatype=XSD.string)
        else:
            srcBool = True
        if not src_spec.startswith("^"):
            bindings["src_spec"] = Literal(src_spec, datatype=XSD.string)
        else:
            srcSpecBool = True
        if not edge_type.startswith("^"):
            bindings["edge_type"] = self.namespace[edge_type]
        else:
            edgeBool = True
        if not dst.startswith("^"):
            bindings["dst_label"] = Literal(dst, datatype=XSD.string)
        else:
            dstBool = True
        for row in self._iter_rows(q, bindings):
            d = row.asdict()
            k = {"@^hasResult": True}
            if srcBool:
                k[src[1:]] = str(d.get("src_label", ""))
            if srcSpecBool:
                k[src_spec[1:]] = str(d.get("src_spec", ""))
            if dstBool:
                k[dst[1:]] = str(d.get("dst_label", ""))
            if edgeBool:
                k[edge_type[1:]] = str(d.get("edge_type", ""))[len(self.namespace):]
            yield k

    def single_edge_dst_binary_capability(self, src, edge_type, verb, subj, obj):
        q = f"""
         SELECT DISTINCT ?src ?edge_type ?dst ?src_label ?verb ?subj ?obj
         WHERE {{
             ?src ?edge_type ?dst.
             ?dst {self.name}:entryPoint ?verb_e.
             ?verb_e rdfs:label ?verb.
             ?dst {self.name}:subject ?subj_e.
             ?subj_e rdfs:label ?subj.
             ?dst {self.name}:d_object ?obj_e.
             ?obj_e rdfs:label ?obj.
             ?src rdfs:label ?src_label.
         }}"""
        bindings = {}
        srcBool = edgeBool = verbBool = subjBool = objBool = False
        if not src.startswith("^"):
            bindings["src_label"] = Literal(src, datatype=XSD.string)
        else:
            srcBool = True
        if not edge_type.startswith("^"):
            bindings["edge_type"] = self.namespace[edge_type]
        else:
            edgeBool = True
        if not verb.startswith("^"):
            bindings["verb"] = Literal(verb, datatype=XSD.string)
        else:
            verbBool = True
        if not subj.startswith("^"):
            bindings["subj"] = Literal(subj, datatype=XSD.string)
        else:
            subjBool = True
        if not obj.startswith("^"):
            bindings["obj"] = Literal(obj, datatype=XSD.string)
        else:
            objBool = True
        for row in self._iter_rows(q, bindings):
            d = row.asdict()
            k = {"@^hasResult": True}
            if srcBool:
                k[src[1:]] = str(d.get("src_label", ""))
            if subjBool:
                k[subj[1:]] = str(d.get("subj", ""))
            if objBool:
                k[obj[1:]] = str(d.get("obj", ""))
            if verbBool:
                k[verb[1:]] = str(d.get("verb", ""))
            if edgeBool:
                k[edge_type[1:]] = str(d.get("edge_type", ""))[len(self.namespace):]
            yield k

    def single_edge_dst_unary_capability(self, src, edge_type, verb, subj):
        q = f"""
         SELECT DISTINCT ?src ?edge_type ?dst ?src_label ?verb ?subj
         WHERE {{
             ?src ?edge_type ?dst.
             ?dst {self.name}:entryPoint ?verb_e.
             ?verb_e rdfs:label ?verb.
             ?dst {self.name}:subject ?subj_e.
             ?subj_e rdfs:label ?subj.
             ?src rdfs:label ?src_label.
         }}"""
        bindings = {}
        srcBool = edgeBool = verbBool = subjBool = False
        if not src.startswith("^"):
            bindings["src_label"] = Literal(src, datatype=XSD.string)
        else:
            srcBool = True
        if not edge_type.startswith("^"):
            bindings["edge_type"] = self.namespace[edge_type]
        else:
            edgeBool = True
        if not verb.startswith("^"):
            bindings["verb"] = Literal(verb, datatype=XSD.string)
        else:
            verbBool = True
        if not subj.startswith("^"):
            bindings["subj"] = Literal(subj, datatype=XSD.string)
        else:
            subjBool = True
        for row in self._iter_rows(q, bindings):
            d = row.asdict()
            k = {"@^hasResult": True}
            if srcBool:
                k[src[1:]] = str(d.get("src_label", ""))
            if subjBool:
                k[subj[1:]] = str(d.get("subj", ""))
            if verbBool:
                k[verb[1:]] = str(d.get("verb", ""))
            if edgeBool:
                k[edge_type[1:]] = str(d.get("edge_type", ""))[len(self.namespace):]
            yield k

    def getOutgoingNodes(self, srcLabel, edgeType):
        S = set()
        for x in self.single_edge(srcLabel, edgeType, "^x"):
            if x["@^hasResult"]:
                S.add(x["x"])
        return S

    def getIngoingNodes(self, dstLabel, edgeType):
        S = set()
        for x in self.single_edge("^x", edgeType, dstLabel):
            if x["@^hasResult"]:
                S.add(x["x"])
        return S

    def single_edge(self, src, edge_type, dst):
        m = re.match(r"(?P<main>[^\[]+)\[(?P<spec>[^\]]+)\]", src)
        if m:
            yield from self.single_edge_src_multipoint(
                m.group("main"), m.group("spec"), edge_type, dst
            )
            return
        m = re.match(
            r"(?P<main>[^\(]+)\((?P<subj>[^\,)]+),(?P<obj>[^\)]+)\)", dst
        )
        if m:
            yield from self.single_edge_dst_binary_capability(
                src, edge_type, m.group("main"), m.group("subj"), m.group("obj")
            )
            return
        m = re.match(r"(?P<main>[^\(]+)\((?P<subj>[^\)]+)\)", dst)
        if m:
            yield from self.single_edge_dst_unary_capability(
                src, edge_type, m.group("main"), m.group("subj")
            )
            return
        q = """
         SELECT DISTINCT ?src ?edge_type ?dst ?src_label ?dst_label
         WHERE {
             ?src ?edge_type ?dst.
             ?src rdfs:label ?src_label.
             ?dst rdfs:label ?dst_label .
         }"""
        bindings = {}
        srcBool = edgeBool = dstBool = False
        if not src.startswith("^"):
            bindings["src_label"] = Literal(src, datatype=XSD.string)
        else:
            srcBool = True
        if not edge_type.startswith("^"):
            bindings["edge_type"] = self.namespace[edge_type]
        else:
            edgeBool = True
        if not dst.startswith("^"):
            bindings["dst_label"] = Literal(dst, datatype=XSD.string)
        else:
            dstBool = True
        for row in self._iter_rows(q, bindings):
            d = row.asdict()
            k = {"@^hasResult": True}
            if srcBool:
                k[src[1:]] = str(d.get("src_label", ""))
            if dstBool:
                k[dst[1:]] = str(d.get("dst_label", ""))
            if edgeBool:
                k[edge_type[1:]] = str(d.get("edge_type", ""))[len(self.namespace):]
            yield k

    def extractPureHierarchy(self, t, flip=False):
        ye = list(self.single_edge("^src", t, "^dst"))
        if not ye:
            return set()
        if flip:
            return {(x["dst"], x["src"]) for x in ye}
        return {(x["src"], x["dst"]) for x in ye}

    def isA(self, src, type):
        q = """
         SELECT DISTINCT ?src ?dst
         WHERE {
             ?src a ?dst.
             ?src rdfs:label ?src_label.
         }"""
        bindings = {}
        srcBool = dstBool = False
        if not src.startswith("^"):
            bindings["src_label"] = Literal(src, datatype=XSD.string)
        else:
            srcBool = True
        if not type.startswith("^"):
            bindings["dst"] = self.namespace[type]
        else:
            dstBool = True
        for row in self._iter_rows(q, bindings):
            d = row.asdict()
            k = {"@^hasResult": True}
            if srcBool:
                k[src[1:]] = str(d.get("src_label", ""))
            if dstBool:
                k[type[1:]] = str(d.get("dst", ""))[len(self.namespace):]
            yield k
