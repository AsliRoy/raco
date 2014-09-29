
# TODO: To be refactored into parallel shared memory lang,
# where you plugin in the parallel shared memory language specific codegen

from raco import algebra
from raco import expression
from raco.language import Algebra
from raco import rules
from raco.pipelines import Pipelined
from raco.language.clangcommon import StagedTupleRef, ct, CBaseLanguage
from raco.language import clangcommon
from raco.utility import emitlist

from raco.algebra import gensym

import logging
_LOG = logging.getLogger(__name__)

import itertools


def readtemplate(fname):
    return clangcommon.readtemplate("grappa_templates", fname)


class GrappaStagedTupleRef(StagedTupleRef):
    def __afterDefinitionCode__(self):
        # Grappa requires structures to be block aligned if they will be
        # iterated over with localizing forall
        return "GRAPPA_BLOCK_ALIGNED"


class GrappaLanguage(CBaseLanguage):
    _base_template = readtemplate("base_query")

    @classmethod
    def base_template(cls):
        return cls._base_template

    @staticmethod
    def log(txt):
        return """LOG(INFO) << "%s";\n""" % txt

    @staticmethod
    def log_unquoted(code, level=0):
        if level == 0:
            log_str = "LOG(INFO)"
        else:
            log_str = "VLOG(%s)" % (level)

        return """%(log_str)s << %(code)s;\n""" % locals()

    @staticmethod
    def group_wrap(ident, grpcode, attrs):
        pipeline_template = ct("""
        Grappa::Metrics::reset();
        auto start_%(ident)s = walltime();
        %(grpcode)s
        auto end_%(ident)s = walltime();
        auto runtime_%(ident)s = end_%(ident)s - start_%(ident)s;
        %(timer_metric)s += runtime_%(ident)s;
        VLOG(1) << "pipeline group %(ident)s: " << runtime_%(ident)s << " s";
        """)

        timer_metric = None
        if attrs['type'] == 'in_memory':
            timer_metric = "in_memory_runtime"
        elif attrs['type'] == 'scan':
            timer_metric = "saved_scan_runtime"

        code = pipeline_template % locals()
        return code

    @staticmethod
    def pipeline_wrap(ident, plcode, attrs):
        code = plcode

        # timing code
        if True:
            inner_code = code
            timing_template = ct("""auto start_%(ident)s = walltime();
            VLOG(1) << "timestamp %(ident)s start " << std::setprecision(15)\
             << start_%(ident)s;
            %(inner_code)s
            auto end_%(ident)s = walltime();
            auto runtime_%(ident)s = end_%(ident)s - start_%(ident)s;
            VLOG(1) << "pipeline %(ident)s: " << runtime_%(ident)s << " s";
            VLOG(1) << "timestamp %(ident)s end " << std::setprecision(15)\
             << end_%(ident)s;
            """)
            code = timing_template % locals()

        dependences = attrs.get('dependences', set())
        assert isinstance(dependences, set)

        _LOG.debug("pipeline %s dependences %s", ident, dependences)
        dependence_code = emitlist([wait_statement(d) for d in dependences])
        dependence_captures = emitlist(
            [",&{dep}".format(dep=d) for d in dependences])

        code = """{dependence_code}
                  {inner_code}
                  """.format(dependence_code=dependence_code,
                             inner_code=code)

        syncname = attrs.get('sync')
        if syncname:
            inner_code = code
            sync_template = ct("""
            CompletionEvent %(syncname)s;
            spawn(&%(syncname)s, [=%(dependence_captures)s] {
                    %(inner_code)s
                    });
                    """)
            code = sync_template % locals()

        return code

    @classmethod
    def compile_stringliteral(cls, st):
        sid = cls.newstringident()
        decl = """int64_t %s;""" % (sid)
        lookup_init = """auto l_%(sid)s = string_index.string_lookup(%(st)s);
                   on_all_cores([=] { %(sid)s = l_%(sid)s; });""" % locals()
        build_init = """
        string_index = build_string_index("sp2bench_1m.index.medium");
        """

        return """(%s)""" % sid, [decl], [build_init, lookup_init]
        # raise ValueError("String Literals not supported in
        # C language: %s" % s)


class GrappaOperator (Pipelined, algebra.Operator):
    _language = GrappaLanguage

    @classmethod
    def new_tuple_ref(cls, sym, scheme):
        return GrappaStagedTupleRef(sym, scheme)

    @classmethod
    def language(cls):
        return cls._language

    def postorder_traversal(self, func):
        return self.postorder(func)


from raco.algebra import UnaryOperator


def create_pipeline_synchronization(state):
    """
    The pipeline_synchronization will sync tasks
    within a single pipeline. Adds this new object to
    the compiler state.
    """
    global_syncname = gensym()

    # true = tracked by gce user metrics
    global_sync_decl_template = ct("""
        GlobalCompletionEvent %(global_syncname)s(true);
        """)
    global_sync_decl = global_sync_decl_template % locals()

    gce_metric_template = """
    GRAPPA_DEFINE_METRIC(CallbackMetric<int64_t>, \
    app_%(pipeline_id)s_gce_incomplete, []{
    return %(global_syncname)s.incomplete();
    });
    """
    pipeline_id = state.getCurrentPipelineId()
    gce_metric_def = gce_metric_template % locals()

    state.addDeclarations([global_sync_decl, gce_metric_def])

    state.setPipelineProperty('global_syncname', global_syncname)
    return global_syncname


# TODO: replace with ScanTemp functionality?
class GrappaMemoryScan(algebra.UnaryOperator, GrappaOperator):
    def num_tuples(self):
        return 10000  # placeholder

    def produce(self, state):
        self.input.produce(state)

    # TODO: when have pipeline tree representation,
    # will have a consumeMaterialized() method instead;
    # for now we reuse the tuple-based consume
    def consume(self, inputsym, src, state):
        # generate the materialization from file into memory

        # scan from index
        # memory_scan_template = """forall_localized( %(inputsym)s_index->vs, \
        # %(inputsym)s_index->nv, [](int64_t ai, Vertex& a) {
        #      forall_here_async<&impl::local_gce>( 0, a.nadj, \
        # [=](int64_t start, int64_t iters) {
        #      for (int64_t i=start; i<start+iters; i++) {
        #        auto %(tuple_name)s = a.local_adj[i];
        #
        #          %(inner_plan_compiled)s
        #       } // end scan over %(inputsym)s (for)
        #       }); // end scan over %(inputsym)s (forall_here_async)
        #       }); // end scan over %(inputsym)s (forall_localized)
        #       """

        global_syncname = create_pipeline_synchronization(state)
        get_pipeline_task_name(state)

        memory_scan_template = ct("""
    forall<&%(global_syncname)s>( %(inputsym)s.data, %(inputsym)s.numtuples, \
    [=](int64_t i, %(tuple_type)s& %(tuple_name)s) {
    %(inner_plan_compiled)s
    }); // end  scan over %(inputsym)s
    """)

        stagedTuple = state.lookupTupleDef(inputsym)
        tuple_type = stagedTuple.getTupleTypename()
        tuple_name = stagedTuple.name

        inner_plan_compiled = self.parent().consume(stagedTuple, self, state)

        code = memory_scan_template % locals()
        state.setPipelineProperty('type', 'in_memory')
        state.setPipelineProperty('source', self.__class__)
        state.addPipeline(code)
        return None

    def shortStr(self):
        return "%s" % (self.opname())

    def __eq__(self, other):
        """
        See important __eq__ notes below
        @see FileScan.__eq__
        """
        return UnaryOperator.__eq__(self, other)


class GrappaSymmetricHashJoin(algebra.Join, GrappaOperator):
    _i = 0

    @classmethod
    def __genBaseName__(cls):
        name = "%03d" % cls._i
        cls._i += 1
        return name

    def __getHashName__(self):
        name = "dhash_%s" % self.symBase
        return name

    def produce(self, state):
        self.symBase = self.__genBaseName__()

        if not isinstance(self.condition, expression.EQ):
            msg = "The C compiler can only handle equi-join conditions\
             of a single attribute: %s" % self.condition
            raise ValueError(msg)

        init_template = ct("""%(hashname)s.init_global_DHT( &%(hashname)s, \
        cores()*16*1024 );
                        """)
        declr_template = ct("""typedef DoubleDHT<%(keytype)s, \
                                                   %(left_in_tuple_type)s, \
                                                   %(right_in_tuple_type)s,
                                                std::hash<%(keytype)s>> \
                    DHT_%(left_in_tuple_type)s_%(right_in_tuple_type)s;
      DHT_%(left_in_tuple_type)s_%(right_in_tuple_type)s %(hashname)s;
      """)

        my_sch = self.scheme()
        left_sch = self.left.scheme()

        # declaration of hash map
        self._hashname = self.__getHashName__()
        hashname = self._hashname
        self.leftTypeRef = state.createUnresolvedSymbol()
        left_in_tuple_type = self.leftTypeRef.getPlaceholder()
        self.rightTypeRef = state.createUnresolvedSymbol()
        right_in_tuple_type = self.rightTypeRef.getPlaceholder()
        hashdeclr = declr_template % locals()

        state.addDeclarationsUnresolved([hashdeclr])

        self.outTuple = GrappaStagedTupleRef(gensym(), my_sch)
        out_tuple_type_def = self.outTuple.generateDefinition()
        state.addDeclarations([out_tuple_type_def])

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(left_sch)
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(left_sch)
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr

        if self.rightCondIsRightAttr:
            self.keypos = self.condition.right.position \
                     - len(left_sch)
            self.keytype = self.language().typename(self.condition.right.typeof(my_sch, None))
        else:
            self.keypos = self.condition.left.position \
                     - len(left_sch)
            self.keytype = self.language().typename(self.condition.left.typeof(my_sch, None))

        self.right.childtag = "right"
        state.addInitializers([init_template % locals()])
        self.right.produce(state)

        self.left.childtag = "left"
        self.left.produce(state)

    def consume(self, t, src, state):
        access_template = ct("""
        %(hashname)s.insert_lookup_iter_%(side)s<&%(global_syncname)s>(\
        %(keyval)s, %(keyname)s, \
        [=](%(other_tuple_type)s %(valname)s) {
            join_coarse_result_count++;
            %(out_tuple_type)s %(out_tuple_name)s = \
                             %(out_tuple_type)s::create<\
                                      %(left_type)s, \
                                      %(right_type)s> (%(left_name)s, \
                            %(right_name)s);
                                %(inner_plan_compiled)s
                                });
                                """)

        hashname = self._hashname
        keyname = t.name
        side = src.childtag

        outTuple = self.outTuple
        out_tuple_type = self.outTuple.getTupleTypename()
        out_tuple_name = self.outTuple.name

        global_syncname = state.getPipelineProperty('global_syncname')

        if src.childtag == "right":
            left_sch = self.left.scheme()

            # save for later
            self.right_in_tuple_type = t.getTupleTypename()
            state.resolveSymbol(self.rightTypeRef, self.right_in_tuple_type)

            keypos = self.keypos
            keyval = t.get_code(keypos)

            inner_plan_compiled = self.parent().consume(outTuple, self, state)

            other_tuple_type = self.leftTypeRef.getPlaceholder()
            left_type = other_tuple_type
            right_type = self.right_in_tuple_type
            left_name = gensym()
            right_name = keyname
            self.right_name = right_name
            valname = left_name

            code = access_template % locals()
            return code

        if src.childtag == "left":
            right_in_tuple_type = self.right_in_tuple_type
            left_in_tuple_type = t.getTupleTypename()
            state.resolveSymbol(self.leftTypeRef, left_in_tuple_type)

            if self.rightCondIsRightAttr:
                keypos = self.condition.left.position
            else:
                keypos = self.condition.right.position

            keyval = t.get_code(keypos)

            inner_plan_compiled = self.parent().consume(outTuple, self, state)

            left_type = left_in_tuple_type
            right_type = self.right_in_tuple_type
            other_tuple_type = self.right_in_tuple_type
            left_name = keyname
            right_name = gensym()
            valname = right_name

            code = access_template % locals()
            return code

        assert False, "src not equal to left or right"


class GrappaShuffleHashJoin(algebra.Join, GrappaOperator):
    _i = 0

    @classmethod
    def __genBaseName__(cls):
        name = "%03d" % cls._i
        cls._i += 1
        return name

    def __getHashName__(self):
        name = "hashjoin_reducer_%s" % self.symBase
        return name

    def produce(self, state):
        left_sch = self.left.scheme()

        self.syncnames = []
        self.symBase = self.__genBaseName__()

        self.right.childtag = "right"
        self.rightTupleTypeRef = None  # may remain None if CSE succeeds
        self.leftTupleTypeRef = None  # may remain None if CSE succeeds

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(left_sch)
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(left_sch)
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr

        # find right key position
        if self.rightCondIsRightAttr:
            self.right_keypos = self.condition.right.position \
                - len(left_sch)
        else:
            self.right_keypos = self.condition.left.position \
                - len(left_sch)

        # find left key position
        if self.rightCondIsRightAttr:
            self.left_keypos = self.condition.left.position
        else:
            self.left_keypos = self.condition.right.position

        # define output tuple
        outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
        out_tuple_type_def = outTuple.generateDefinition()
        out_tuple_type = outTuple.getTupleTypename()
        out_tuple_name = outTuple.name

        # common index is defined by same right side and same key
        # TODO: probably want also left side
        hashtableInfo = state.lookupExpr((self.right, self.right_keypos))
        if not hashtableInfo:
            # if right child never bound then store hashtable symbol and
            # call right child produce
            self._hashname = self.__getHashName__()
            _LOG.debug("generate hashname %s for %s", self._hashname, self)

            hashname = self._hashname

            # declaration of hash map
            self.rightTupleTypeRef = state.createUnresolvedSymbol()
            self.leftTupleTypeRef = state.createUnresolvedSymbol()
            self.outTupleTypeRef = state.createUnresolvedSymbol()
            right_type = self.rightTupleTypeRef.getPlaceholder()
            left_type = self.leftTupleTypeRef.getPlaceholder()

            # TODO: really want this addInitializers to be addPreCode
            # TODO: *for all pipelines that use this hashname*
            init_template = ct("""
            auto %(hashname)s_num_reducers = cores();
            auto %(hashname)s = allocateJoinReducers\
            <int64_t,%(left_type)s,%(right_type)s,%(out_tuple_type)s>
                (%(hashname)s_num_reducers);
            auto %(hashname)s_ctx = HashJoinContext<int64_t,%(left_type)s,
                %(right_type)s,%(out_tuple_type)s>
                (%(hashname)s, %(hashname)s_num_reducers);""")

            state.addInitializers([init_template % locals()])
            self.right.produce(state)

            self.left.childtag = "left"
            self.left.produce(state)

            state.saveExpr((self.right, self.right_keypos),
                           (self._hashname, right_type, left_type,
                            self.right_syncname, self.left_syncname))

        else:
            # if found a common subexpression on right child then
            # use the same hashtable
            self._hashname, right_type, left_type,\
                self.right_syncname, self.left_syncname = hashtableInfo
            _LOG.debug("reuse hash %s for %s", self._hashname, self)

        # now that Relation is produced, produce its contents by iterating over
        # the join result
        iterate_template = ct("""MapReduce::forall_symmetric
        <&%(pipeline_sync)s>
        (%(hashname)s, &JoinReducer<int64_t,%(left_type)s,
        %(right_type)s,%(out_tuple_type)s>::resultAccessor,
            [=](%(out_tuple_type)s& %(out_tuple_name)s) {
                 %(inner_code_compiled)s
            });
        """)

        hashname = self._hashname

        state.addDeclarations([out_tuple_type_def])

        pipeline_sync = create_pipeline_synchronization(state)
        get_pipeline_task_name(state)

        # add dependences on left and right inputs
        state.addToPipelinePropertySet('dependences', self.right_syncname)
        state.addToPipelinePropertySet('dependences', self.left_syncname)

        # reduce is a single self contained pipeline.
        # future hashjoin implementations may pipeline out of it
        # by passing a continuation to reduceExecute
        reduce_template = ct("""
        %(hashname)s_ctx.reduceExecute();

        """)
        state.addPreCode(reduce_template % locals())

        delete_template = ct("""
            freeJoinReducers(%(hashname)s, %(hashname)s_num_reducers);""")
        state.addPostCode(delete_template % locals())

        inner_code_compiled = self.parent().consume(outTuple, self, state)

        code = iterate_template % locals()
        state.setPipelineProperty('type', 'in_memory')
        state.setPipelineProperty('source', self.__class__)
        state.addPipeline(code)

    def consume(self, inputTuple, fromOp, state):
        if fromOp.childtag == "right":
            side = "Right"
            self.right_syncname = get_pipeline_task_name(state)

            keypos = self.right_keypos

            self.rightTupleTypename = inputTuple.getTupleTypename()
            if self.rightTupleTypeRef is not None:
                state.resolveSymbol(self.rightTupleTypeRef,
                                    self.rightTupleTypename)
        elif fromOp.childtag == "left":
            side = "Left"
            self.left_syncname = get_pipeline_task_name(state)

            keypos = self.left_keypos

            self.leftTupleTypename = inputTuple.getTupleTypename()
            if self.leftTupleTypeRef is not None:
                state.resolveSymbol(self.leftTupleTypeRef,
                                    self.leftTupleTypename)
        else:
            assert False, "src not equal to left or right"

        hashname = self._hashname
        keyname = inputTuple.name
        keytype = inputTuple.getTupleTypename()
        keyval = inputTuple.get_code(keypos)

        # intra-pipeline sync
        global_syncname = state.getPipelineProperty('global_syncname')

        mat_template = ct("""%(hashname)s_ctx.emitIntermediate%(side)s\
                <&%(global_syncname)s>(\
                %(keyval)s, %(keyname)s);""")

        # materialization point
        code = mat_template % locals()
        return code


class GrappaGroupBy(clangcommon.BaseCGroupby, GrappaOperator):
    _i = 0

    _ONE_BUILT_IN = 0
    _MULTI_UDA = 1

    @classmethod
    def __genHashName__(cls):
        name = "group_hash_%03d" % cls._i
        cls._i += 1
        return name

    def produce(self, state):
        self._agg_mode = None
        if len(self.aggregate_list) == 1 and isinstance(self.aggregate_list[0], expression.BuiltinAggregateExpression):
            self._agg_mode = self._ONE_BUILT_IN
        elif all([isinstance(a, expression.UdaAggregateExpression)
                 for a in self.aggregate_list]):
            self._agg_mode = self._MULTI_UDA

        assert self._agg_mode is not None, "unsupported aggregates {0}".format(self.aggregate_list)
        _LOG.debug("%s _agg_mode was set to %s", self, self._agg_mode)

        self.useKey = len(self.grouping_list) > 0
        _LOG.debug("groupby uses keys? %s" % self.useKey)

        inp_sch = self.input.scheme()

        if self._agg_mode == self._ONE_BUILT_IN:
            state_type = self.language().typename(self.aggregate_list[0].input.typeof(inp_sch, None))
            op = self.aggregate_list[0].__class__.__name__
            self.update_func = "Aggregates::{op}<{type}, {type}>".format(op=op, type=state_type)
        elif self._agg_mode == self._MULTI_UDA:
            # for now just name the aggregate after the first state variable
            self.func_name = self.updaters[0][0]
            self.state_tuple = GrappaStagedTupleRef(gensym(), self.state_scheme)
            state.addDeclarations([self.state_tuple.generateDefinition()])
            state_type = self.state_tuple.getTupleTypename()
            self.update_func = "{name}_update".format(name=self.func_name)

        update_func = self.update_func

        if self.useKey:
            numkeys = len(self.grouping_list)
            keytype = "std::tuple<{types}>".format(types=','.join([self.language().typename(g.typeof(inp_sch, None)) for g in self.grouping_list]))

        self._hashname = self.__genHashName__()
        _LOG.debug("generate hashname %s for %s", self._hashname, self)

        hashname = self._hashname

        if self.useKey:
            init_template = """auto %(hashname)s = \
            DHT_symmetric<{keytype},{valtype},hash_tuple::hash<{keytype}>>::create_DHT_symmetric( );""".format(keytype=keytype,
                                                                                                       valtype=state_type)

        else:
            if self._agg_mode == self._ONE_BUILT_IN:
                initial_value = self.__get_initial_value__(0, cached_inp_sch=inp_sch)
                no_key_state_initializer = "counter<{state_type}>::create({valinit})".format(state_type=state_type, valinit=initial_value)
            elif self._agg_mode == self._MULTI_UDA:
                no_key_state_initializer = \
                    "symmetric_global_alloc<{state_tuple_type}>()".format(
                        state_tuple_type=self.state_tuple.getTupleTypename())

            init_template = ct("""auto %(hashname)s = {initializer};
            """.format(initializer=no_key_state_initializer))

        state.addInitializers([init_template % locals()])

        self.input.produce(state)

        # now that everything is aggregated, produce the tuples
        #assert len(self.column_list()) == 1 \
        #    or isinstance(self.column_list()[0],
        #                  expression.AttributeRef), \
        #    """assumes first column is the key and second is aggregate result
#            column_list: %s""" % self.column_list()

        if self.useKey:
            mapping_var_name = gensym()
            if self._agg_mode == self._ONE_BUILT_IN:
                emit_type = self.language().typename(self.aggregate_list[0].input.typeof(self.input.scheme(), None))
            elif self._agg_mode == self._MULTI_UDA:
                emit_type = self.state_tuple.getTupleTypename()

            initializer_list = ["%(mapping_var_name)s.first"]

            if self._agg_mode == self._ONE_BUILT_IN:
                # pass in attribute values as a tuple
                initializer_template = "std::tuple_cat( {values} )"
                # need to force type in make_tuple
                initializer_list += ["std::make_tuple(%(mapping_var_name)s.second)"]
            elif self._agg_mode == self._MULTI_UDA:
                # pass in attribute values individually
                initializer_template = "{values}"
                initializer_list += ["%(mapping_var_name)s.second"]

            initializer = initializer_template.format(values=','.join(initializer_list))

            produce_template = """%(hashname)s->\
                forall_entries<&%(pipeline_sync)s>\
                ([=](std::pair<const {keytype},%(emit_type)s>& %(mapping_var_name)s) {{
                    %(output_tuple_type)s %(output_tuple_name)s({initializer});
                    %(inner_code)s
                    }});
                    """.format(initializer=initializer, keytype=keytype)

        else:
            if self._agg_mode == self._ONE_BUILT_IN:
                template_args = "{state_type}, counter, &{update_func}, &get_count".format(state_type=state_type,
                                                                                           update_func=update_func)
                output_template = """%(output_tuple_type)s %(output_tuple_name)s;
                %(output_tuple_set_func)s(%(output_tuple_name)s_tmp);"""

            elif self._agg_mode == self._MULTI_UDA:
                template_args = "{state_type}, &{update_func}".format(state_type=state_type,
                                                                      update_func=update_func)
                output_template = """%(output_tuple_type)s %(output_tuple_name)s = %(output_tuple_type)s::create(%(output_tuple_name)s_tmp);"""

            produce_template = """auto %(output_tuple_name)s_tmp = \
            reduce<%(template_args)s>(%(hashname)s);

            {output_template}
            %(inner_code)s
            """.format(output_template=output_template)


        pipeline_sync = create_pipeline_synchronization(state)
        get_pipeline_task_name(state)

        # add a dependence on the input aggregation pipeline
        state.addToPipelinePropertySet('dependences', self.input_syncname)

        output_tuple = GrappaStagedTupleRef(gensym(), self.scheme())
        output_tuple_name = output_tuple.name
        output_tuple_type = output_tuple.getTupleTypename()
        output_tuple_set_func = output_tuple.set_func_code(0)
        state.addDeclarations([output_tuple.generateDefinition()])

        inner_code = self.parent().consume(output_tuple, self, state)
        code = produce_template % locals()
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)

    def consume(self, inputTuple, fromOp, state):
        # save the inter-pipeline task name
        self.input_syncname = get_pipeline_task_name(state)

        inp_sch = self.input.scheme()

        all_decls = []
        all_inits = []

        # compile update statements
        def compile_assignments(assgns):
            state_var_update_template = "auto {assignment};"
            state_var_updates = []
            state_vars = []
            decls = []
            inits = []

            for a in assgns:
                state_name, update_exp = a
                # doesn't have to use inputTuple.name, but it will for simplicity
                rhs = self.language().compile_expression(update_exp,
                                                         tupleref=inputTuple,
                                                         state_scheme=self.state_scheme)
                # combine lhs, rhs with assignment
                code = "{lhs} = {rhs}".format(lhs=state_name, rhs=rhs[0])

                decls += rhs[1]
                inits += rhs[2]

                state_var_updates.append(
                    state_var_update_template.format(assignment=code))
                state_vars.append(state_name)

            return state_var_updates, state_vars, decls, inits

        update_updates, update_state_vars, update_decls, update_inits = compile_assignments(self.updaters)
        init_updates, init_state_vars, init_decls, init_inits = compile_assignments(self.inits)
        assert set(update_state_vars) == set(init_state_vars), "Initialized and update state vars are not the same (may not need to be?)"
        all_decls += update_decls + init_decls
        all_inits += update_inits + init_inits

        if self._agg_mode == self._MULTI_UDA:
            state_tuple_decl = self.state_tuple.generateDefinition()
            update_def = readtemplate('update_definition').format(
                state_type=self.state_tuple.getTupleTypename(),
                input_type=inputTuple.getTupleTypename(),
                input_tuple_name=inputTuple.name,
                state_var_updates=emitlist(update_updates),
                state_vars=','.join(update_state_vars),
                name=self.func_name)
            init_def = readtemplate('init_definition').format(
                state_type=self.state_tuple.getTupleTypename(),
                state_var_updates=emitlist(init_updates),
                state_vars=','.join(init_state_vars),
                name=self.func_name)

            all_decls += [update_def, init_def]

        # form code to fill in the materialize template
        if self._agg_mode == self._ONE_BUILT_IN:
            init_func = "Aggregates::Zero"

            if isinstance(self.aggregate_list[0], expression.ZeroaryOperator):
                # no value needed for Zero-input aggregate,
                # but just provide the first column
                valpos = 0
            elif isinstance(self.aggregate_list[0], expression.UnaryOperator):
                # get value positions from aggregated attributes
                valpos = self.aggregate_list[0].input.get_position(self.scheme())
            else:
                assert False, "only support Unary or Zeroary aggregates"

            update_val = inputTuple.get_code(valpos)
            input_type = self.language().typename(self.aggregate_list[0].input.typeof(inp_sch, None))

        elif self._agg_mode == self._MULTI_UDA:
            init_func = "{name}_init".format(name=self.func_name)
            update_val = "{tuple_name}".format(tuple_name=inputTuple.name)
            input_type = inputTuple.getTupleTypename()

        if self.useKey:
            numkeys = len(self.grouping_list)
            keygets = ','.join([inputTuple.get_code(g.get_position(inp_sch))
                                for g in self.grouping_list])

            # need to force types in std::make_tuple
            materialize_template = """%(hashname)s->update\
                <&%(pipeline_sync)s, %(input_type)s, \
                &%(update_func)s,&%(init_func)s>(\
                std::make_tuple({keygets}), %(update_val)s);
          """.format(keygets=keygets)
        else:
            if self._agg_mode == self._ONE_BUILT_IN:
                materialize_template = """%(hashname)s->count = \
                %(update_func)s(%(hashname)s->count, \
                                          %(update_val)s);
                """
            elif self._agg_mode == self._MULTI_UDA:
                materialize_template = """
                auto %(hashname)s_local_ptr = %(hashname)s.localize();
                *%(hashname)s_local_ptr = \
                %(update_func)s(*%(hashname)s_local_ptr, %(update_val)s);
                """

        hashname = self._hashname
        tuple_name = inputTuple.name
        pipeline_sync = state.getPipelineProperty("global_syncname")

        state.addDeclarations(all_decls)
        state.addInitializers(all_inits)

        update_func = self.update_func

        code = materialize_template % locals()
        return code


def wait_statement(name):
    return """{name}.wait();""".format(name=name)


def get_pipeline_task_name(state):
    name = "p_task_{n}".format(n=state.getCurrentPipelineId())
    state.setPipelineProperty('sync', name)
    wait_stmt = wait_statement(name)
    state.addMainWaitStatement(wait_stmt)
    return name


class GrappaHashJoin(algebra.Join, GrappaOperator):
    _i = 0

    @classmethod
    def __genHashName__(cls):
        name = "hash_%03d" % cls._i
        cls._i += 1
        return name

    @classmethod
    def __aggregate_val__(cls, tuple, cols):
        return "std::make_tuple({0})".format(
            ','.join([tuple.get_code(p) for p in cols]))

    def __aggregate_type__(cls, sch, cols):
        return "std::tuple<{0}>".format(
            ','.join([cls.language().typename(
                expression.UnnamedAttributeRef(c).typeof(sch, None)) for c in cols]))

    def produce(self, state):
        declr_template = ct("""typedef MatchesDHT<%(keytype)s, \
                          %(in_tuple_type)s, hash_tuple::hash<%(keytype)s>> \
                           DHT_%(in_tuple_type)s;
        DHT_%(in_tuple_type)s %(hashname)s;
        """)


        self.right.childtag = "right"
        self.rightTupleTypeRef = None  # may remain None if CSE succeeds

        my_sch = self.scheme()
        left_sch = self.left.scheme()
        right_sch = self.right.scheme()

        self.leftcols, self.rightcols = algebra.convertcondition(self.condition,
                                               len(left_sch),
                                               left_sch+right_sch)

        keytype = self.__aggregate_type__(my_sch, self.rightcols)

        # common index is defined by same right side and same key
        hashtableInfo = state.lookupExpr((self.right, frozenset(self.rightcols)))
        if not hashtableInfo:
            # if right child never bound then store hashtable symbol and
            # call right child produce
            self._hashname = self.__genHashName__()
            _LOG.debug("generate hashname %s for %s", self._hashname, self)

            hashname = self._hashname

            # declaration of hash map
            self.rightTupleTypeRef = state.createUnresolvedSymbol()
            in_tuple_type = self.rightTupleTypeRef.getPlaceholder()
            hashdeclr = declr_template % locals()
            state.addDeclarationsUnresolved([hashdeclr])

            init_template = ct("""%(hashname)s.init_global_DHT( &%(hashname)s,
            cores()*16*1024 );""")
            state.addInitializers([init_template % locals()])
            self.right.produce(state)
            state.saveExpr((self.right, frozenset(self.rightcols)),
                           (self._hashname, self.rightTupleTypename,
                            self.right_syncname))
            # TODO always safe here? I really want to call
            # TODO saveExpr before self.right.produce(),
            # TODO but I need to get the self.rightTupleTypename cleanly
        else:
            # if found a common subexpression on right child then
            # use the same hashtable
            self._hashname, self.rightTupleTypename, self.right_syncname\
                = hashtableInfo
            _LOG.debug("reuse hash %s for %s", self._hashname, self)

        self.left.childtag = "left"
        self.left.produce(state)

    def consume(self, t, src, state):
        if src.childtag == "right":

            right_template = ct("""
            %(hashname)s.insert_async<&%(pipeline_sync)s>(\
            %(keyval)s, %(keyname)s);
            """)

            hashname = self._hashname
            keyname = t.name
            keyval = self.__aggregate_val__(t, self.rightcols)

            self.right_syncname = get_pipeline_task_name(state)

            self.rightTupleTypename = t.getTupleTypename()
            if self.rightTupleTypeRef is not None:
                state.resolveSymbol(self.rightTupleTypeRef,
                                    self.rightTupleTypename)

            pipeline_sync = state.getPipelineProperty('global_syncname')

            # materialization point
            code = right_template % locals()

            return code

        if src.childtag == "left":
            left_template = ct("""
            %(hashname)s.lookup_iter<&%(pipeline_sync)s>( \
            %(keyval)s, \
            [=](%(right_tuple_type)s& %(right_tuple_name)s) {
              join_coarse_result_count++;
              %(out_tuple_type)s %(out_tuple_name)s = \
               %(out_tuple_type)s::create<\
                       %(input_tuple_type)s, \
                       %(right_tuple_type)s> \
                           (%(keyname)s, %(right_tuple_name)s);
              %(inner_plan_compiled)s
            });
     """)

            # add a dependence on the right pipeline
            state.addToPipelinePropertySet('dependences', self.right_syncname)

            hashname = self._hashname
            keyname = t.name
            input_tuple_type = t.getTupleTypename()
            keyval = self.__aggregate_val__(t, self.leftcols)

            pipeline_sync = state.getPipelineProperty('global_syncname')


            right_tuple_name = gensym()
            right_tuple_type = self.rightTupleTypename

            outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
            out_tuple_type_def = outTuple.generateDefinition()
            out_tuple_type = outTuple.getTupleTypename()
            out_tuple_name = outTuple.name

            state.addDeclarations([out_tuple_type_def])

            inner_plan_compiled = self.parent().consume(outTuple, self, state)

            code = left_template % locals()
            return code

        assert False, "src not equal to left or right"


def indentby(code, level):
    indent = " " * ((level + 1) * 6)
    return "\n".join([indent + line for line in code.split("\n")])

#
#
#
# class FreeMemory(GrappaOperator):
#  def fire(self, expr):
#    for ref in noReferences(expr)


# Basic selection like serial C++
class GrappaSelect(clangcommon.CSelect, GrappaOperator):
    pass


# Basic apply like serial C++
class GrappaApply(clangcommon.CApply, GrappaOperator):
    pass


# Basic duplication based bag union like serial C++
class GrappaUnionAll(clangcommon.CUnionAll, GrappaOperator):
    pass


# Basic materialized copy based project like serial C++
class GrappaProject(clangcommon.CProject, GrappaOperator):
    pass


class GrappaFileScan(clangcommon.CFileScan, GrappaOperator):
    ascii_scan_template_GRAPH = """
          {
            tuple_graph tg;
            tg = readTuples( "%(name)s" );

            FullEmpty<GlobalAddress<Graph<Vertex>>> f1;
            privateTask( [&f1,tg] {
              f1.writeXF( Graph<Vertex>::create(tg, /*directed=*/true) );
            });
            auto l_%(resultsym)s_index = f1.readFE();

            on_all_cores([=] {
              %(resultsym)s_index = l_%(resultsym)s_index;
            });
        }
        """

    # C++ type inference cannot infer T in readTuples<T>;
    # we resolve it later, so use %%
    ascii_scan_template = """
    {
    if (FLAGS_bin) {
    %(resultsym)s = readTuplesUnordered<%%(result_type)s>( \
    FLAGS_input_file_%(name)s + ".bin" );
    } else {
    %(resultsym)s.data = readTuples<%%(result_type)s>( \
    FLAGS_input_file_%(name)s, FLAGS_nt);
    %(resultsym)s.numtuples = FLAGS_nt;
    auto l_%(resultsym)s = %(resultsym)s;
    on_all_cores([=]{ %(resultsym)s = l_%(resultsym)s; });
    }
    }
    """

    def __get_ascii_scan_template__(self):
        return self.ascii_scan_template

    def __get_binary_scan_template__(self):
        _LOG.warn("binary not currently supported\
         for GrappaLanguage, emitting ascii")
        return self.ascii_scan_template

    def __get_relation_decl_template__(self, name):
        return """
            DEFINE_string(input_file_%(name)s, "%(name)s", "Input file");
            Relation<%(tuple_type)s> %(resultsym)s;
            """


class GrappaStore(clangcommon.BaseCStore, GrappaOperator):
    def __file_code__(self, t, state):
        my_sch = self.scheme()

        filename = (str(self.relation_key).split(":")[2])
        outputnamedecl = """\
        DEFINE_string(output_file, "%s.bin", "Output File");""" % filename
        state.addDeclarations([outputnamedecl])
        names = [x.encode('UTF8') for x in my_sch.get_names()]
        schemefile = 'writeSchema("%s", FLAGS_output_file+".scheme");\n' % \
                     (zip(names, my_sch.get_types()),)
        state.addPreCode(schemefile)
        resultfile = 'writeTuplesUnordered(&result, FLAGS_output_file+".bin");'
        state.addPipelineFlushCode(resultfile)

        return ""


class MemoryScanOfFileScan(rules.Rule):
    """A rewrite rule for making a scan into materialization
     in memory then memory scan"""
    def fire(self, expr):
        if isinstance(expr, algebra.Scan) \
                and not isinstance(expr, GrappaFileScan):
            return GrappaMemoryScan(GrappaFileScan(expr.relation_key,
                                                   expr.scheme()))
        return expr

    def __str__(self):
        return "Scan => MemoryScan(FileScan)"


def grappify(join_type, emit_print):
    return [
        rules.ProjectingJoinToProjectOfJoin(),

        rules.OneToOne(algebra.Select, GrappaSelect),
        MemoryScanOfFileScan(),
        rules.OneToOne(algebra.Apply, GrappaApply),
        rules.OneToOne(algebra.Join, join_type),
        rules.OneToOne(algebra.GroupBy, GrappaGroupBy),
        rules.OneToOne(algebra.Project, GrappaProject),
        rules.OneToOne(algebra.UnionAll, GrappaUnionAll),
        # TODO: obviously breaks semantics
        rules.OneToOne(algebra.Union, GrappaUnionAll),
        clangcommon.StoreToBaseCStore(emit_print, GrappaStore),

        #clangcommon.BreakHashJoinConjunction(GrappaSelect, join_type)
    ]


class GrappaAlgebra(Algebra):
    def __init__(self, emit_print=clangcommon.EMIT_CONSOLE):
        self.emit_print = emit_print

    def opt_rules(self, **kwargs):
        # datalog_rules = [
        #     # rules.removeProject(),
        #     rules.CrossProduct2Join(),
        #     rules.SimpleGroupBy(),
        #     # SwapJoinSides(),
        #     rules.OneToOne(algebra.Select, GrappaSelect),
        #     rules.OneToOne(algebra.Apply, GrappaApply),
        #     # rules.OneToOne(algebra.Scan,MemoryScan),
        #     MemoryScanOfFileScan(),
        #     # rules.OneToOne(algebra.Join, GrappaSymmetricHashJoin),
        #     rules.OneToOne(algebra.Join, self.join_type),
        #     rules.OneToOne(algebra.Project, GrappaProject),
        #     rules.OneToOne(algebra.GroupBy, GrappaGroupBy),
        #     # TODO: this Union obviously breaks semantics
        #     rules.OneToOne(algebra.Union, GrappaUnionAll),
        #     rules.OneToOne(algebra.Store, GrappaStore)
        #     # rules.FreeMemory()
        # ]

        join_type = kwargs.get('join_type', GrappaHashJoin)

        # sequence that works for myrial
        rule_grps_sequence = [
            rules.remove_trivial_sequences,
            rules.simple_group_by,
            clangcommon.clang_push_select,
            rules.push_project,
            rules.push_apply,
            grappify(join_type, self.emit_print)
        ]

        if kwargs.get('SwapJoinSides'):
            rule_grps_sequence.insert(0, [rules.SwapJoinSides()])

        return list(itertools.chain(*rule_grps_sequence))
