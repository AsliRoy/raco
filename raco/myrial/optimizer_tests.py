
import collections
import random

from raco.algebra import *
from raco.expression import NamedAttributeRef as AttRef
from raco.language import MyriaAlgebra
from raco.algebra import LogicalAlgebra
from raco.compile import optimize

import raco.expression as expression
import raco.scheme as scheme
import raco.myrial.myrial_test as myrial_test

class OptimizerTest(myrial_test.MyrialTestCase):

    x_scheme = scheme.Scheme([("a", "int"),("b", "int"), ("c", "int")])
    y_scheme = scheme.Scheme([("d", "int"),("e", "int"), ("f", "int")])
    x_key = "public:adhoc:X"
    y_key = "public:adhoc:Y"

    def setUp(self):
        super(OptimizerTest, self).setUp()

        random.seed(387) # make results deterministic
        rng = 20
        count = 30
        self.x_data = collections.Counter(
            [(random.randrange(rng), random.randrange(rng),
              random.randrange(rng)) for x in range(count)])
        self.y_data = collections.Counter(
            [(random.randrange(rng), random.randrange(rng),
              random.randrange(rng)) for x in range(count)])

        self.db.ingest(OptimizerTest.x_key,
                       self.x_data,
                       OptimizerTest.x_scheme)
        self.db.ingest(OptimizerTest.y_key,
                       self.y_data,
                       OptimizerTest.y_scheme)

        self.expected = collections.Counter(
            [(a,b,c,d,e,f) for (a,b,c) in self.x_data
             for (d,e,f) in self.y_data if a > b and e <= f and c==d])

    @staticmethod
    def logical_to_physical(lp):
        physical_plans = optimize([('root', lp)],
                                  target=MyriaAlgebra,
                                  source=LogicalAlgebra)
        return physical_plans[0][1]

    @staticmethod
    def get_count(op, claz):
        """Return the count of operator instances within an operator tree."""

        def count(_op):
            if isinstance(_op, claz):
                yield 1
            else:
                yield 0
        return sum(op.postorder(count))

    @staticmethod
    def get_num_select_conjuncs(op):
        """Get the number of conjuntions within all select operations."""
        def count(_op):
            if isinstance(_op, Select):
                yield len(expression.extract_conjuncs(_op.condition))
            else:
                yield 0
        return sum(op.postorder(count))

    def test_merge_selects(self):
        lp = StoreTemp('OUTPUT',
               Select(expression.LTEQ(AttRef("e"), AttRef("f")),
                 Select(expression.EQ(AttRef("c"),AttRef("d")),
                   Select(expression.GT(AttRef("a"),AttRef("b")),
                      CrossProduct(Scan(self.x_key, self.x_scheme),
                                   Scan(self.y_key, self.y_scheme))))))

        self.assertEquals(self.get_count(lp, Select), 3)
        self.assertEquals(self.get_count(lp, CrossProduct), 1)

        pp = self.logical_to_physical(lp)
        self.assertEquals(self.get_count(pp, Select), 1)
        self.assertEquals(self.get_count(pp, CrossProduct), 0)

        self.db.evaluate(pp)
        result = self.db.get_temp_table('OUTPUT')
        self.assertEquals(result, self.expected)


    def test_extract_join(self):
        """Extract a join condition from the middle of complex select."""
        s = expression.AND(expression.LTEQ(AttRef("e"), AttRef("f")),
                           expression.AND(
                               expression.EQ(AttRef("c"),AttRef("d")),
                               expression.GT(AttRef("a"),AttRef("b"))))

        lp = StoreTemp('OUTPUT', Select(s, CrossProduct(
            Scan(self.x_key, self.x_scheme),
            Scan(self.y_key, self.y_scheme))))

        self.assertEquals(self.get_num_select_conjuncs(lp), 3)

        pp = self.logical_to_physical(lp)
        self.assertEquals(self.get_count(pp, Select), 1)
        self.assertEquals(self.get_count(pp, CrossProduct), 0)

        # One select condition should get folded into the join
        self.assertEquals(self.get_num_select_conjuncs(lp), 2)

        self.db.evaluate(pp)
        result = self.db.get_temp_table('OUTPUT')
        self.assertEquals(result, self.expected)

    def test_multi_condition_join(self):
        s = expression.AND(expression.EQ(AttRef("c"),AttRef("d")),
                           expression.EQ(AttRef("a"),AttRef("f")))

        lp = StoreTemp('OUTPUT', Select(s, CrossProduct(
            Scan(self.x_key, self.x_scheme),
            Scan(self.y_key, self.y_scheme))))

        self.assertEquals(self.get_num_select_conjuncs(lp), 2)

        pp = self.logical_to_physical(lp)
        self.assertEquals(self.get_count(pp, CrossProduct), 0)
        self.assertEquals(self.get_count(pp, Select), 0)

        expected = collections.Counter(
            [(a,b,c,d,e,f) for (a,b,c) in self.x_data
             for (d,e,f) in self.y_data if a==f and c==d])

        self.db.evaluate(pp)
        result = self.db.get_temp_table('OUTPUT')
        self.assertEquals(result, expected)

    def test_multiway_join(self):

        e = collections.Counter([(1,2),(2,3),(1,2),(3,4)])
        self.db.ingest('public:adhoc:Z', e, scheme.Scheme(
            [('src','int'),('dst','int')]))

        query = """
        T = SCAN(public:adhoc:Z);
        U = [FROM T1=T, T2=T, T3=T WHERE T1.dst==T2.src AND T2.dst==T3.src
             EMIT x=T1.src, y=T3.dst];
        DUMP(U);
        """

        statements = self.parser.parse(query)
        self.processor.evaluate(statements)

        lp = self.processor.get_logical_plan()
        self.assertEquals(self.get_count(lp, CrossProduct), 2)

        pp = self.logical_to_physical(lp)
        self.assertEquals(self.get_count(pp, CrossProduct), 0)

        self.db.evaluate(pp)

        result = self.db.get_temp_table('__OUTPUT0__')

        expected = collections.Counter(
            [(s1, d3) for (s1, d1) in e.elements() for (s2, d2) in e.elements()
             for (s3, d3) in e.elements() if d1==s2 and d2==s3])

        self.assertEquals(result, expected)
