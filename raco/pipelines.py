# TODO: make it pass with flake8 test
# flake8: noqa

import abc
from raco.utility import emitlist
from algebra import gensym

import logging
LOG = logging.getLogger(__name__)

class ResolvingSymbol:
    def __init__(self, name):
        self._name = name
        self._placeholder = "%%(%s)s" % name

    def getPlaceholder(self):
        return self._placeholder

    def getName(self):
        return self._name

class CompileState:

    def __init__(self, lang, cse=True):
        self.language = lang

        self.declarations = []
        self.declarations_later = []
        self.pipelines = []
        self.scan_pipelines = []
        self.flush_pipelines = []
        self.initializers = []
        self.pipeline_count = 0

        # { expression => symbol for materialized result }
        self.materialized = {}

        # { symbol => tuple type definition }
        self.tupledefs = {}

        # symbol resolution
        self.resolving_symbols = {}

        self.common_subexpression_elim = cse

        self.current_pipeline_properties = {}
        self.current_pipeline_precode = []
        self.current_pipeline_postcode = []

    def setPipelineProperty(self, key, value):
        LOG.debug("set %s in %s" % (key, self.current_pipeline_properties))
        self.current_pipeline_properties[key] = value

    def getPipelineProperty(self, key):
        LOG.debug("get %s from %s" % (key, self.current_pipeline_properties))
        return self.current_pipeline_properties[key]

    def checkPipelineProperty(self, key):
        """
        Like getPipelineProperty but returns None if no property is found
        """
        LOG.debug("get(to check) %s from %s" % (key, self.current_pipeline_properties))
        return self.current_pipeline_properties.get(key)

    def createUnresolvedSymbol(self):
        name = gensym()
        rs = ResolvingSymbol(name)
        self.resolving_symbols[name] = None
        return rs

    def resolveSymbol(self, rs, value):
        self.resolving_symbols[rs.getName()] = value

    def addDeclarations(self, d):
        self.declarations += d

    def addDeclarationsUnresolved(self, d):
        """
        Ordered in the code after the regular declarations
        just so that any name dependences already have been declared
        ALTERNATIVE: split decls into forward decls and definitions
        """
        self.declarations_later += d


    def addInitializers(self, i):
        self.initializers += i

    def addPipeline(self, p):
        LOG.debug("output pipeline %s", self.current_pipeline_properties)
        pipeline_code = emitlist(self.current_pipeline_precode) +\
                        self.language.pipeline_wrap(self.pipeline_count, p, self.current_pipeline_properties) +\
                        emitlist(self.current_pipeline_postcode)

        # force scan pipelines to go first
        if self.current_pipeline_properties.get('type') == 'scan':
            self.scan_pipelines.append(pipeline_code)
        else:
            self.pipelines.append(pipeline_code)

        self.pipeline_count += 1
        self.current_pipeline_properties = {}
        self.current_pipeline_precode = []
        self.current_pipeline_postcode = []

    def addCode(self, c):
        """
        Just add code here
        """
        self.pipelines.append(c)

    def addPreCode(self, c):
        self.current_pipeline_precode.append(c)

    def addPostCode(self, c):
        self.current_pipeline_postcode.append(c)

    def addPipelineFlushCode(self, c):
        self.flush_pipelines.append(c)

    def getInitCode(self):
        # inits is a set
        # If this ever becomes a bottleneck when declarations are strings,
        # as in clang, then resort to at least symbol name deduping.
        #TODO: better would be to mark elements of self.initializers as
        #TODO: "do dedup" or "don't dedup"
        s = set()
        def f(x):
            if x in s: return False
            else:
                s.add(x)
                return True

        code = emitlist(filter(f,self.initializers))
        return code % self.resolving_symbols

    def getDeclCode(self):
        # declarations is a set
        # If this ever becomes a bottleneck when declarations are strings,
        # as in clang, then resort to at least symbol name deduping.
        s = set()
        def f(x):
            if x in s: return False
            else:
                s.add(x)
                return True

        # keep in original order
        code = emitlist(filter(f, self.declarations))
        code += emitlist(filter(f, self.declarations_later))
        return code % self.resolving_symbols

    def getExecutionCode(self):
        # list -> string
        scan_linearized = emitlist(self.scan_pipelines)
        mem_linearized = emitlist(self.pipelines)
        flush_linearized = emitlist(self.flush_pipelines)
        scan_linearized_wrapped = self.language.group_wrap(gensym(), scan_linearized, {'type': 'scan'})
        mem_linearized_wrapped = self.language.group_wrap(gensym(), mem_linearized, {'type': 'in_memory'})

        linearized = scan_linearized_wrapped + mem_linearized_wrapped + flush_linearized

        # substitute all lazily resolved symbols
        resolved = linearized % self.resolving_symbols

        return resolved

    def lookupExpr(self, expr):

        if self.common_subexpression_elim:
            res = self.materialized.get(expr)
            LOG.debug("lookup subexpression %s -> %s", expr, res)
            return res
        else:
            # if CSE is turned off then always return None for expression matches
            return None

    def saveExpr(self, expr, sym):
        LOG.debug("saving subexpression %s -> %s", expr, sym)
        self.materialized[expr] = sym

    def lookupTupleDef(self, sym):
        return self.tupledefs.get(sym)

    def saveTupleDef(self, sym, tupledef):
        self.tupledefs[sym] = tupledef


class Pipelined(object):
    """
    Trait to provide the compilePipeline method
    for calling into pipeline style compilation.
    """

    __metaclass__ = abc.ABCMeta

    def __markAllParents__(self):
      root = self

      def markChildParent(op):
        for c in op.children():
          c.parent = op
        return []

      [_ for _ in root.postorder(markChildParent)]

    @abc.abstractmethod
    def produce(self, state):
      """Denotation for producing a tuple"""
      return

    @abc.abstractmethod
    def consume(self, inputTuple, fromOp, state):
      """Denotation for consuming a tuple"""
      return

    # emitprint: quiet, console, file
    def compilePipeline(self):
      self.__markAllParents__()

      state = CompileState(self.language)

      state.addCode( self.language.comment("Compiled subplan for %s" % self) )

      self.produce(state)

      #state.addCode( self.language.log("Evaluating subplan %s" % self) )

      return state
