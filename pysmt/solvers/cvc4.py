#
# This file is part of pySMT.
#
#   Copyright 2014 Andrea Micheli and Marco Gario
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
from __future__ import absolute_import

from six.moves import xrange

from pysmt.exceptions import SolverAPINotFound

try:
    import CVC4
except ImportError:
    raise SolverAPINotFound

import pysmt.typing as types
from pysmt.logics import PYSMT_LOGICS, ARRAYS_CONST_LOGICS, QF_LIA, QF_LRA, QF_BV

from pysmt.solvers.solver import Solver, Converter, SolverOptions
from pysmt.exceptions import (SolverReturnedUnknownResultError,
                              InternalSolverError,
                              NonLinearError, PysmtValueError,
                              PysmtTypeError)
from pysmt.walkers import DagWalker
from pysmt.solvers.smtlib import SmtLibBasicSolver, SmtLibIgnoreMixin
from pysmt.solvers.eager import EagerModel
from pysmt.decorators import catch_conversion_error
from pysmt.constants import Fraction, is_pysmt_integer, to_python_integer
from pysmt.solvers.interpolation import Interpolator


class CVC4Options(SolverOptions):

    def __init__(self, **base_options):
        SolverOptions.__init__(self, **base_options)
        # TODO: CVC4 Supports UnsatCore extraction
        # but we did not wrapped it yet. (See #349)
        if self.unsat_cores_mode is not None:
            raise PysmtValueError("'unsat_cores_mode' option not supported.")

    @staticmethod
    def _set_option(cvc4, name, value):
        try:
            cvc4.setOption(name, CVC4.SExpr(value))
        except:
            raise PysmtValueError("Error setting the option '%s=%s'" % (name,value))

    def __call__(self, solver):
        if solver.logic_name == "QF_SLIA":
            self._set_option(solver.cvc4,
                             "strings-exp", "true")

        self._set_option(solver.cvc4,
                         "produce-models", str(self.generate_models).lower())
        self._set_option(solver.cvc4,
                         "incremental", str(self.incremental).lower())
        if self.random_seed is not None:
            self._set_option(solver.cvc4,
                             "random-seed", str(self.random_seed))

        for k,v in self.solver_options.items():
            self._set_option(solver.cvc4, str(k), str(v))

# EOC CVC4Options


class CVC4Solver(Solver, SmtLibBasicSolver, SmtLibIgnoreMixin):

    LOGICS = PYSMT_LOGICS -\
             ARRAYS_CONST_LOGICS -\
             set(l for l in PYSMT_LOGICS if not l.theory.linear)

    OptionsClass = CVC4Options

    def __init__(self, environment, logic, **options):
        Solver.__init__(self,
                        environment=environment,
                        logic=logic,
                        **options)
        self.em = CVC4.ExprManager()

        self.cvc4 = None
        self.declarations = None
        self.logic_name = str(logic)
        if "t" in self.logic_name:
            # Custom Type extension
            self.logic_name = self.logic_name.replace("t","")
        if self.logic_name == "QF_BOOL":
            self.logic_name = "QF_LRA"
        elif self.logic_name == "BOOL":
            self.logic_name = "LRA"

        self.reset_assertions()
        self.converter = CVC4Converter(environment, cvc4_exprMgr=self.em)
        return

    def reset_assertions(self):
        del self.cvc4
        # CVC4's SWIG interface is not acquiring ownership of the
        # SmtEngine object. Forcing it here.
        self.cvc4 = CVC4.SmtEngine(self.em); self.cvc4.thisown=1
        self.options(self)
        self.declarations = set()
        self.cvc4.setLogic(self.logic_name)

    def declare_variable(self, var):
        raise NotImplementedError

    def add_assertion(self, formula, named=None):
        self._assert_is_boolean(formula)
        term = self.converter.convert(formula)
        self.cvc4.assertFormula(term)
        return

    def get_model(self):
        assignment = {}
        for s in self.environment.formula_manager.get_all_symbols():
            if s.is_term():
                if s.symbol_type().is_custom_type(): continue
                v = self.get_value(s)
                assignment[s] = v
        return EagerModel(assignment=assignment, environment=self.environment)

    def solve(self, assumptions=None):
        if assumptions is not None:
            conj_assumptions = self.environment.formula_manager.And(assumptions)
            cvc4_assumption = self.converter.convert(conj_assumptions)
            res = self.cvc4.checkSat(cvc4_assumption)
        else:
            try:
                res = self.cvc4.checkSat()
            except:
                raise InternalSolverError()

        # Convert returned type
        res_type = res.isSat()
        if res_type == CVC4.Result.SAT_UNKNOWN:
            raise SolverReturnedUnknownResultError()
        else:
            return res_type == CVC4.Result.SAT
        return

    def push(self, levels=1):
        if not self.options.incremental:
            # The exceptions from CVC4 are not raised correctly
            # (probably due to the wrapper)
            # Therefore, we need to check that we can actually do a push
            raise NotImplementedError("The solver is not incremental")

        for _ in xrange(levels):
            self.cvc4.push()
        return

    def pop(self, levels=1):
        for _ in xrange(levels):
            self.cvc4.pop()
        return

    def print_model(self, name_filter=None):
        if name_filter is None:
            var_set = self.declarations
        else:
            var_set = (var for var in self.declarations\
                       if name_filter(var))
        for var in var_set:
            print("%s = %s", (var.symbol_name(), self.get_value(var)))
        return

    def get_value(self, item):
        self._assert_no_function_type(item)
        term = self.converter.convert(item)
        cvc4_res = self.cvc4.getValue(term)
        res = self.converter.back(cvc4_res)
        if self.environment.stc.get_type(item).is_real_type() and \
           self.environment.stc.get_type(res).is_int_type():
            res = self.environment.formula_manager.Real(Fraction(res.constant_value(), 1))
        return res

    def _exit(self):
        del self.cvc4

    def set_option(self, name, value):
        """Sets an option.

        :param name and value: Option to be set
        :type name: String
        :type value: String
        """
        self.cvc4.setOption(name, CVC4.SExpr(value))


class CVC4Converter(Converter, DagWalker):

    def __init__(self, environment, cvc4_exprMgr):
        DagWalker.__init__(self, environment)

        self.cvc4_exprMgr = cvc4_exprMgr
        self.mkExpr = cvc4_exprMgr.mkExpr
        self.mkConst = cvc4_exprMgr.mkConst
        self.mkVar = cvc4_exprMgr.mkVar
        self.realType = cvc4_exprMgr.realType()
        self.intType = cvc4_exprMgr.integerType()
        self.boolType = cvc4_exprMgr.booleanType()
        self.stringType = cvc4_exprMgr.stringType()

        self.declared_vars = {}
        self.rev_declared_vars = {}
        self.backconversion = {}
        self.mgr = environment.formula_manager
        self._get_type = environment.stc.get_type

        # Back Conversion
        self.back_memoization = {}
        self.back_fun = {
            CVC4.AND: self._back_adapter(self.mgr.And),
            CVC4.OR:  self._back_adapter(self.mgr.Or),
            CVC4.NOT: self._back_adapter(self.mgr.Not),
            CVC4.IMPLIES: self._back_adapter(self.mgr.Implies),
            CVC4.ITE: self._back_adapter(self.mgr.Ite),
            CVC4.EQUAL:  self._back_adapter(self.mgr.Equals),
            CVC4.LEQ: self._back_adapter(self.mgr.LE),
            CVC4.PLUS: self._back_adapter(self.mgr.Plus),
            CVC4.MULT: self._back_adapter(self.mgr.Times),
            CVC4.BITVECTOR_MULT: self._back_adapter(self.mgr.BVMul),
            CVC4.BITVECTOR_PLUS: self._back_adapter(self.mgr.BVAdd),
            CVC4.BITVECTOR_UDIV: self._back_adapter(self.mgr.BVUDiv),
            CVC4.BITVECTOR_UREM: self._back_adapter(self.mgr.BVURem),
            CVC4.BITVECTOR_CONCAT: self._back_adapter(self.mgr.BVConcat),
            CVC4.BITVECTOR_OR: self._back_adapter(self.mgr.BVOr),
            CVC4.BITVECTOR_XOR:self._back_adapter(self.mgr.BVXor),
            CVC4.BITVECTOR_AND:self._back_adapter(self.mgr.BVAnd),
            CVC4.BITVECTOR_NOT:self._back_adapter(self.mgr.BVNot),
            CVC4.BITVECTOR_SUB:self._back_adapter(self.mgr.BVSub),
            CVC4.BITVECTOR_NEG:self._back_adapter(self.mgr.BVNeg),
            CVC4.BITVECTOR_SREM:self._back_adapter(self.mgr.BVSRem),
            CVC4.BITVECTOR_SDIV:self._back_adapter(self.mgr.BVSDiv),
            CVC4.BITVECTOR_ULT: self._back_adapter(self.mgr.BVULT),
            CVC4.BITVECTOR_SLT: self._back_adapter(self.mgr.BVSLT),
            CVC4.BITVECTOR_ULE: self._back_adapter(self.mgr.BVULE),
            CVC4.BITVECTOR_SLE: self._back_adapter(self.mgr.BVSLE),
            CVC4.BITVECTOR_SHL: self._back_adapter(self.mgr.BVLShl),
            CVC4.BITVECTOR_LSHR: self._back_adapter(self.mgr.BVLShr),
            CVC4.BITVECTOR_ASHR: self._back_adapter(self.mgr.BVAShr),
            CVC4.BITVECTOR_COMP: self._back_adapter(self.mgr.BVComp),
            CVC4.SELECT: self._back_adapter(self.mgr.Select),
            CVC4.STORE: self._back_adapter(self.mgr.Store),
            # Slightly more complex conversion
            CVC4.BITVECTOR_EXTRACT: self._back_bv_extract,
            CVC4.BITVECTOR_ZERO_EXTEND: self._back_bv_zext,
            CVC4.BITVECTOR_SIGN_EXTEND: self._back_bv_sext,
            CVC4.BITVECTOR_ROTATE_LEFT: self._back_bv_rol,
            CVC4.BITVECTOR_ROTATE_RIGHT: self._back_bv_ror
        }

        return

    def declare_variable(self, var):
        if not var.is_symbol():
            raise PysmtTypeError("Trying to declare as a variable something "
                                 "that is not a symbol: %s" % var)
        if var not in self.declared_vars:
            cvc4_type = self._type_to_cvc4(var.symbol_type())
            decl = self.mkVar(var.symbol_name(), cvc4_type)
            self.declared_vars[var] = decl
            self.rev_declared_vars[decl.toString()] = var

    def _back_adapter(self, op):
        """Create a function that for the given op.

        This is used in the construction of back_fun, to simplify the code.
        """
        def back_apply(term, args):
            return op(*args)
        return back_apply

    def _back_constant(self, expr):
        res = None
        if expr.isConst():
            if expr.getType().isBoolean():
                v = expr.getConstBoolean()
                res = self.mgr.Bool(v)
            elif expr.getType().isInteger():
                v = expr.getConstRational().toString()
                res = self.mgr.Int(int(v))
            elif expr.getType().isReal():
                v = expr.getConstRational().toString()
                res = self.mgr.Real(Fraction(v))
            elif expr.getType().isBitVector():
                bv = expr.getConstBitVector()
                v = bv.getValue().toString()
                width = bv.getSize()
                res = self.mgr.BV(int(v), width)
            elif expr.getType().isString():
                v = expr.getConstString()
                res = self.mgr.String(v.toString())
            elif expr.getType().isArray():
                const_ = expr.getConstArrayStoreAll()
                array_type = self._cvc4_type_to_type(const_.getType())
                base_value = self.back(const_.getExpr())
                res = self.mgr.Array(array_type.index_type,
                                     base_value)
            else:
                raise PysmtTypeError("Unsupported constant type:",
                                     expr.getType().toString())
        else:
            raise PysmtTypeError("Unsupported expression:", expr.toString())

        return res

    def _back_bv_extract(self, term, args):
        '''
        Converts a CVC4 bit-vector extract back to a PySMT object.
        '''
        op = term.getOperator()
        assert op.getKind() == CVC4.BITVECTOR_EXTRACT_OP
        ext = op.getConstBitVectorExtract()
        return self.mgr.BVExtract(args[0], ext.low, ext.high)

    def _back_bv_zext(self, term, args):
        op = term.getOperator()
        assert op.getKind() == CVC4.BITVECTOR_ZERO_EXTEND_OP
        zext = op.getConstBitVectorZeroExtend()
        return self.mgr.BVZExt(args[0], zext.zeroExtendAmount)

    def _back_bv_sext(self, term, args):
        op = term.getOperator()
        assert op.getKind() == CVC4.BITVECTOR_SIGN_EXTEND_OP
        sext = op.getConstBitVectorSignExtend()
        return self.mgr.BVSExt(args[0], sext.signExtendAmount)

    def _back_bv_rol(self, term, args):
        op = term.getOperator()
        assert op.getKind() == CVC4.BITVECTOR_ROTATE_LEFT_OP
        rol = op.getConstBitVectorRotateLeft()
        return self.mgr.BVRol(args[0], rol.rotateLeftAmount)

    def _back_bv_ror(self, term, args):
        op = term.getOperator()
        assert op.getKind() == CVC4.BITVECTOR_ROTATE_RIGHT_OP
        rol = op.getConstBitVectorRotateRight()
        return self.mgr.BVRor(args[0], rol.rotateRightAmount)

    def _back_single_term(self, term, mgr, args):
        """Builds the pysmt formula given a term and the list of formulae
        obtained by converting the term children.

        :param term: The CVC4 term to be transformed in pysmt formulae
        :type term: CVC4 term

        :param mgr: The formula manager to be sued to build the
        formulae, it should allow for type unsafety.
        :type mgr: Formula manager

        :param args: List of the pysmt formulae obtained by converting
        all the args (obtained by mathsat.msat_term_get_arg()) to
        pysmt formulae
        :type args: List of pysmt formulae

        :returns The pysmt formula representing the given term
        :rtype Pysmt formula
        """

        if not args:
            if term.isVariable():
                name = term.toString()
                assert name in self.rev_declared_vars, 'should be a declared symbol'
                return self.rev_declared_vars[name]
            else:
                assert term.isConst(), 'should be constant if not variable'
                return self._back_constant(term)
        else:
            try:
                return self.back_fun[term.getKind()](term, args)
            except KeyError:
                raise ConvertExpressionError("Unsupported expression:",
                                             term.toString())

    def _walk_back(self, term, mgr):
        stack = [term]

        while len(stack) > 0:
            current = stack.pop()
            current_id = current.getId()
            if current_id not in self.back_memoization:
                self.back_memoization[current_id] = None
                stack.append(current)
                children = current.getChildren()
                for c in current.getChildren():
                    stack.append(c)
            elif self.back_memoization[current_id] is None:
                converted_args=[self.back_memoization[c.getId()] for c in current.getChildren()]
                res = self._back_single_term(current, mgr, converted_args)
                self.back_memoization[current_id] = res
            else:
                # we already visited the node, nothing else to do
                pass
        return self.back_memoization[term.getId()]

    def back(self, expr):
        return self._walk_back(expr, self.mgr)

    @catch_conversion_error
    def convert(self, formula):
        return self.walk(formula)

    def walk_and(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.AND, args)

    def walk_or(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.OR, args)

    def walk_not(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.NOT, args[0])

    def walk_symbol(self, formula, args, **kwargs):
        #pylint: disable=unused-argument
        if formula not in self.declared_vars:
            self.declare_variable(formula)
        return self.declared_vars[formula]

    def walk_iff(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.EQUAL, args[0], args[1])

    def walk_implies(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.IMPLIES, args[0], args[1])

    def walk_le(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.LEQ, args[0], args[1])

    def walk_lt(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.LT, args[0], args[1])

    def walk_ite(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.ITE, args[0], args[1], args[2])

    def walk_real_constant(self, formula, **kwargs):
        frac = formula.constant_value()
        n,d = frac.numerator, frac.denominator
        rep = str(n) + "/" + str(d)
        return self.mkConst(CVC4.Rational(rep))

    def walk_int_constant(self, formula, **kwargs):
        assert is_pysmt_integer(formula.constant_value())
        rep = str(formula.constant_value())
        return self.mkConst(CVC4.Rational(rep))

    def walk_bool_constant(self, formula, **kwargs):
        return self.cvc4_exprMgr.mkBoolConst(formula.constant_value())

    def walk_exists(self, formula, args, **kwargs):
        (bound_formula, var_list) = \
                 self._rename_bound_variables(args[0], formula.quantifier_vars())
        bound_vars_list = self.mkExpr(CVC4.BOUND_VAR_LIST, var_list)
        return self.mkExpr(CVC4.EXISTS,
                           bound_vars_list,
                           bound_formula)

    def walk_forall(self, formula, args, **kwargs):
        (bound_formula, var_list) = \
                 self._rename_bound_variables(args[0], formula.quantifier_vars())
        bound_vars_list = self.mkExpr(CVC4.BOUND_VAR_LIST, var_list)
        return self.mkExpr(CVC4.FORALL,
                           bound_vars_list,
                           bound_formula)

    def walk_plus(self, formula, args, **kwargs):
        res = self.mkExpr(CVC4.PLUS, args)
        return res

    def walk_array_store(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STORE, args[0], args[1], args[2])

    def walk_array_select(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.SELECT, args[0], args[1])

    def walk_minus(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.MINUS, args[0], args[1])

    def walk_equals(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.EQUAL, args[0], args[1])

    def walk_times(self, formula, args, **kwargs):
        if sum(1 for x in formula.args() if x.get_free_variables()) > 1:
            raise NonLinearError(formula)
        res = args[0]
        for x in args[1:]:
            res = self.mkExpr(CVC4.MULT, res, x)
        return res

    def walk_toreal(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.TO_REAL, args[0])

    def walk_function(self, formula, args, **kwargs):
        name = formula.function_name()
        if name not in self.declared_vars:
            self.declare_variable(name)
        decl = self.declared_vars[name]
        return self.mkExpr(CVC4.APPLY_UF, decl, args)

    def walk_bv_constant(self, formula, **kwargs):
        vrepr = str(formula.constant_value())
        width = formula.bv_width()
        return self.mkConst(CVC4.BitVector(width, CVC4.Integer(vrepr)))

    def walk_bv_ult(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_ULT, args[0], args[1])

    def walk_bv_ule(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_ULE, args[0], args[1])

    def walk_bv_concat(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_CONCAT, args[0], args[1])

    def walk_bv_extract(self, formula, args, **kwargs):
        ext = self.mkConst(CVC4.BitVectorExtract(formula.bv_extract_end(),
                                                 formula.bv_extract_start()))
        return self.mkExpr(CVC4.BITVECTOR_EXTRACT, ext, args[0])

    def walk_bv_or(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_OR, args[0], args[1])

    def walk_bv_not(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_NOT, args[0])

    def walk_bv_and(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_AND, args[0], args[1])

    def walk_bv_xor(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_XOR, args[0], args[1])

    def walk_bv_add(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_PLUS, args[0], args[1])

    def walk_bv_sub(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_SUB, args[0], args[1])

    def walk_bv_neg(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_NEG, args[0])

    def walk_bv_mul(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_MULT, args[0], args[1])

    def walk_bv_tonatural(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_TO_NAT, args[0])

    def walk_bv_udiv(self, formula, args, **kwargs):
        # Force deterministic semantics of division by 0
        # If the denominator is bv0, then the result is ~0
        n,d = args
        if d.isConst():
            bv = d.getConstBitVector()
            v = bv.getValue().toString()
            if v == "0":
                return self.mkExpr(CVC4.BITVECTOR_NOT, d)
            else:
                return self.mkExpr(CVC4.BITVECTOR_UDIV, n, d)
        else:
            # (d == 0) ? ~0 : n bvudiv d
            base = self.mkExpr(CVC4.BITVECTOR_UDIV, n, d)
            zero = self.mkConst(CVC4.BitVector(formula.bv_width(),
                                               CVC4.Integer("0")))
            notzero = self.mkExpr(CVC4.BITVECTOR_NOT, zero)
            test = self.mkExpr(CVC4.EQUAL, d, zero)
            return self.mkExpr(CVC4.ITE, test, notzero, base)

    def walk_bv_urem(self, formula, args, **kwargs):
        # Force deterministic semantics of reminder by 0
        # If the denominator is bv0, then the result is the numerator
        n,d = args
        if d.isConst():
            bv = d.getConstBitVector()
            v = bv.getValue().toString()
            if v == "0":
                return n
            else:
                return self.mkExpr(CVC4.BITVECTOR_UREM, n, d)
        else:
            # (d == 0) ? n : n bvurem d
            base = self.mkExpr(CVC4.BITVECTOR_UREM, n, d)
            zero = self.mkConst(CVC4.BitVector(formula.bv_width(),
                                               CVC4.Integer("0")))
            test = self.mkExpr(CVC4.EQUAL, d, zero)
            return self.mkExpr(CVC4.ITE, test, n, base)

    def walk_bv_lshl(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_SHL, args[0], args[1])

    def walk_bv_lshr(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_LSHR, args[0], args[1])

    def walk_bv_rol(self, formula, args, **kwargs):
        ext = self.mkConst(CVC4.BitVectorRotateLeft(formula.bv_rotation_step()))
        return self.mkExpr(CVC4.BITVECTOR_ROTATE_LEFT, ext, args[0])

    def walk_bv_ror(self, formula, args, **kwargs):
        ext = self.mkConst(CVC4.BitVectorRotateRight(formula.bv_rotation_step()))
        return self.mkExpr(CVC4.BITVECTOR_ROTATE_RIGHT, ext, args[0])

    def walk_bv_zext(self, formula, args, **kwargs):
        ext = self.mkConst(CVC4.BitVectorZeroExtend(formula.bv_extend_step()))
        return self.mkExpr(CVC4.BITVECTOR_ZERO_EXTEND, ext, args[0])

    def walk_bv_sext (self, formula, args, **kwargs):
        ext = self.mkConst(CVC4.BitVectorSignExtend(formula.bv_extend_step()))
        return self.mkExpr(CVC4.BITVECTOR_SIGN_EXTEND, ext, args[0])

    def walk_bv_slt(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_SLT, args[0], args[1])

    def walk_bv_sle(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_SLE, args[0], args[1])

    def walk_bv_comp(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_COMP, args[0], args[1])

    def walk_bv_sdiv(self, formula, args, **kwargs):
        # Force deterministic semantics of division by 0
        # If the denominator is bv0, then the result is:
        #   * ~0 (if the numerator is signed >= 0)
        #   * 1 (if the numerator is signed < 0)
        n,d = args
        # sign_expr : ( 0 s<= n ) ? ~0 : 1 )
        zero = self.mkConst(CVC4.BitVector(formula.bv_width(),
                                           CVC4.Integer("0")))
        notzero = self.mkExpr(CVC4.BITVECTOR_NOT, zero)
        one = self.mkConst(CVC4.BitVector(formula.bv_width(),
                                          CVC4.Integer("1")))
        is_gt_zero = self.mkExpr(CVC4.BITVECTOR_SLE, zero, n)
        sign_expr = self.mkExpr(CVC4.ITE, is_gt_zero, notzero, one)
        base = self.mkExpr(CVC4.BITVECTOR_SDIV, n, d)
        if d.isConst():
            v = d.getConstBitVector().getValue().toString()
            if v == "0":
                return sign_expr
            else:
                return base
        else:
            # (d == 0) ? sign_expr : base
            is_zero = self.mkExpr(CVC4.EQUAL, d, zero)
            return self.mkExpr(CVC4.ITE, is_zero, sign_expr, base)

    def walk_bv_srem(self, formula, args, **kwargs):
        # Force deterministic semantics of reminder by 0
        # If the denominator is bv0, then the result is the numerator
        n,d = args
        if d.isConst():
            v = d.getConstBitVector().getValue().toString()
            if v == "0":
                return n
            else:
                return self.mkExpr(CVC4.BITVECTOR_SREM, n, d)
        else:
            # (d == 0) ? n : n bvurem d
            base = self.mkExpr(CVC4.BITVECTOR_SREM, n, d)
            zero = self.mkConst(CVC4.BitVector(formula.bv_width(),
                                               CVC4.Integer("0")))
            test = self.mkExpr(CVC4.EQUAL, d, zero)
            return self.mkExpr(CVC4.ITE, test, n, base)

    def walk_bv_ashr(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.BITVECTOR_ASHR, args[0], args[1])

    def walk_str_constant(self, formula, args, **kwargs):
        return self.mkConst(CVC4.CVC4String(formula.constant_value()))

    def walk_str_length (self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_LENGTH , args[0])

    def walk_str_concat(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_CONCAT, args)

    def walk_str_contains(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_STRCTN, args[0], args[1])

    def walk_str_indexof(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_STRIDOF, args[0], args[1], args[2])

    def walk_str_replace(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_STRREPL, args[0], args[1], args[2])

    def walk_str_substr(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_SUBSTR, args[0], args[1], args[2])

    def walk_str_prefixof(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_PREFIX, args[0], args[1])

    def walk_str_suffixof(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_SUFFIX, args[0], args[1])

    def walk_str_to_int(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_STOI, args[0])

    def walk_int_to_str(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_ITOS, args[0])

    def walk_str_charat(self, formula, args, **kwargs):
        return self.mkExpr(CVC4.STRING_CHARAT, args[0], args[1])

    def _type_to_cvc4(self, tp):
        if tp.is_bool_type():
            return self.boolType
        elif tp.is_real_type():
            return self.realType
        elif tp.is_int_type():
            return self.intType
        elif tp.is_function_type():
            stps = [self._type_to_cvc4(x) for x in tp.param_types]
            rtp = self._type_to_cvc4(tp.return_type)
            return self.cvc4_exprMgr.mkFunctionType(stps, rtp)
        elif tp.is_array_type():
            # Recursively convert the types of index and elem
            idx_cvc_type = self._type_to_cvc4(tp.index_type)
            elem_cvc_type = self._type_to_cvc4(tp.elem_type)
            return self.cvc4_exprMgr.mkArrayType(idx_cvc_type, elem_cvc_type)
        elif tp.is_bv_type():
            return self.cvc4_exprMgr.mkBitVectorType(tp.width)
        elif tp.is_string_type():
            return self.stringType
        elif tp.is_custom_type():
            return self.cvc4_exprMgr.mkSort(str(tp))
        else:
            raise NotImplementedError("Unsupported type: %s" %tp)

    def _cvc4_type_to_type(self, type_):
        if type_.isBoolean():
            return types.BOOL
        elif type_.isInteger():
            return types.INT
        elif type_.isReal():
            return types.REAL
        elif type_.isArray():
            # Casting Type into ArrayType
            type_ = CVC4.ArrayType(type_)
            # Recursively convert the types of index and elem
            idx_type = self._cvc4_type_to_type(type_.getIndexType())
            elem_type = self._cvc4_type_to_type(type_.getConstituentType())
            return types.ArrayType(idx_type, elem_type)
        elif type_.isBitVector():
            # Casting Type into BitVectorType
            type_ = CVC4.BitVectorType(type_)
            return types.BVType(type_.getSize())
        elif type_.isFunction():
            # Casting Type into FunctionType
            type_ = CVC4.FunctionType(type_)
            return_type = type_.getRangeType()
            param_types = tuple(self._cvc4_type_to_type(ty) for ty in type_.getArgTypes())
            return types.FunctionType(return_type, param_types)
        else:
            raise NotImplementedError("Unsupported type: %s" % type_)

    def _rename_bound_variables(self, formula, variables):
        """Bounds the variables in formula.

        Returns a tuple (new_formula, new_var_list) in which the old
        variables have been replaced by the new variables in the list.
        """
        mkBoundVar = self.cvc4_exprMgr.mkBoundVar
        new_var_list = [mkBoundVar(x.symbol_name(),
                                   self._type_to_cvc4(x.symbol_type())) \
                        for x in variables]
        old_var_list = [self.walk_symbol(x, []) for x in variables]
        new_formula = formula.substitute(old_var_list, new_var_list)
        return (new_formula, new_var_list)

class CVC4Interpolator(Interpolator):

    LOGICS = [QF_LIA, QF_LRA, QF_BV]

    def __init__(self, environment, logic=None):
        Interpolator.__init__(self)
        self.em = CVC4.ExprManager()
        self.cvc4 = CVC4.SmtEngine(self.em); self.cvc4.thisown=1
        self.converter = CVC4Converter(environment, cvc4_exprMgr=self.em)
        self.logic_name = str(logic)
        self.cvc4.setLogic(self.logic_name)
        self.cvc4.setOption('sygus-interpol', CVC4.SExpr('conclusion'))
        self.cvc4.setOption("incremental", CVC4.SExpr("false"));
        self.cvc4.setOption("check-synth-sol", CVC4.SExpr("true"));

    def reset_assertions(self):
        del self.cvc4
        # CVC4's SWIG interface is not acquiring ownership of the
        # SmtEngine object. Forcing it here.
        self.cvc4 = CVC4.SmtEngine(self.em); self.cvc4.thisown=1
        self.cvc4.setLogic(self.logic_name)
        self.cvc4.setOption('sygus-interpol', CVC4.SExpr('conclusion'))
        self.cvc4.setOption("incremental", CVC4.SExpr("false"));
        self.cvc4.setOption("check-synth-sol", CVC4.SExpr("true"));

    def _exit(self):
        del self.cvc4
        del self.em

    def _check_logic(self, formulas):
        for f in formulas:
            logic = get_logic(f, self.environment)
            ok = any(logic <= l for l in self.LOGICS)
            if not ok:
                raise PysmtValueError("Logic not supported by CVC4 "
                                      "interpolation. (detected logic is: %s)" \
                                      % str(logic))

    def binary_interpolant(self, a, b):
        self.reset_assertions()
        self.cvc4.assertFormula(self.converter.convert(a))
        res = self.cvc4.checkSat([self.converter.convert(b)])

        if res.isSat():
            return None

        I = self.cvc4.getInterpolant()
        return self.converter.back(self.cvc4.getInterpolant())

    def sequence_interpolant(self, formulas):
        raise NotImplementedError("sequence interpolants not supported by CVC4 interpolator yet")
