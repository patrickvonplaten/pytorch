# Owner(s): ["module: inductor"]

import torch
from functorch import make_fx
from torch._dynamo.utils import counters
from torch._higher_order_ops.auto_functionalize import auto_functionalized
from torch._inductor.fx_passes.reinplace import reinplace_inplaceable_ops_core
from torch._inductor.test_case import run_tests, TestCase as InductorTestCase
from torch.testing._internal.common_device_type import (
    instantiate_device_type_tests,
    onlyCUDA,
)
from torch.testing._internal.inductor_utils import HAS_CPU, HAS_GPU


aten = torch.ops.aten


const = torch.tensor(0.0)
device = "cuda"


def num_reinplacing_failures():
    return counters["inductor"]["possibly_missed_reinplacing_opportunities"]


@torch.library.custom_op("_reinplacing::sin", mutates_args={"out"})
def sin(x: torch.Tensor, out: torch.Tensor) -> None:
    out.copy_(x.sin())


@torch.library.custom_op("_reinplacing::sin_cos", mutates_args={"out_sin", "out_cos"})
def sin_cos(x: torch.Tensor, out_sin: torch.Tensor, out_cos: torch.Tensor) -> None:
    out_sin.copy_(x.sin())
    out_cos.copy_(x.cos())


class TestReinplacingPassCorrectness(InductorTestCase):
    def _test(self, f):
        nf = torch.compile(f)
        inp = (
            torch.randn(4, device=device),
            torch.ones(2, device=device, dtype=torch.int),
        )
        inp2 = (inp[0].clone(), inp[1].clone())
        self.assertEqual(f(*inp), nf(*inp2))
        self.assertEqual(inp, inp2)

    @onlyCUDA
    def test_dont_modify_live(self, device):
        def f(x, y):
            x = x.cos()
            x2 = x.index_put((y,), const)
            return x2, x

        self._test(f)

    @onlyCUDA
    def test_dont_modify_view_of_live(self, device):
        def f(x, y):
            x = x.cos()
            x2 = aten.alias(x)
            x2 = x2.index_put((y,), const)
            y = x2 + x.cos()
            return y

        self._test(f)

    @onlyCUDA
    def test_dont_modify_input(self, device):
        def f(x, y):
            return x.index_put((y,), const)

        self._test(f)

    @onlyCUDA
    def test_should_modify_inner(self, device):
        def f(x, y):
            x = x.cos()
            x = x.index_put((y,), const)
            return x

        self._test(f)

    @onlyCUDA
    def test_should_modify_input(self, device):
        def f(x, y):
            x = x.index_put_((y,), const)
            return x

        self._test(f)

    def test_counters(self, device):
        counters.clear()

        def f(x):
            out = torch.empty_like(x)
            _, new_out = auto_functionalized(sin._opoverload, x=x, out=out)
            y = out * new_out
            return new_out, y

        x = torch.randn(3, device=device)
        gm = make_fx(f, tracing_mode="fake")(x)
        reinplace_inplaceable_ops_core(gm.graph)

        # We shouldn't have been able to reinplace `out` because it was used after
        # auto_functionalized. Note that this usually doesn't happen in practice;
        # we're artificially creating this example to test the counter.
        # IF THIS NUMBER GOES TO ZERO, PLEASE FIND ANOTHER EXAMPLE
        self.assertEqual(num_reinplacing_failures(), 1)

    def test_multi_output_intermediate(self, device):
        for requires_grad in [False, True]:
            counters.clear()

            def f(x):
                out1 = torch.empty_like(x)
                out2 = torch.empty_like(x)
                sin_cos(x, out1, out2)
                return out1, out2, x**2

            x = torch.randn(3, device=device, requires_grad=requires_grad)
            res1, res2, _ = torch.compile(f)(x)
            self.assertEqual(res1, x.sin())
            self.assertEqual(res2, x.cos())
            self.assertEqual(num_reinplacing_failures(), 0)

    def test_multiple_mutations(self, device):
        counters.clear()

        def f(x, out):
            sin(x, out)
            sin(out, out)
            sin(out, out)
            return out

        x = torch.randn(3, device=device)
        out = torch.randn(3, device=device)
        result = torch.compile(f)(x, out)
        self.assertEqual(result, x.sin().sin().sin())
        self.assertEqual(result, out)
        self.assertEqual(num_reinplacing_failures(), 0)

    def test_multiple_intermediate(self, device):
        counters.clear()

        def f(x):
            out = torch.empty_like(x)
            sin(x, out)
            sin(out, out)
            sin(out, out)
            return out

        x = torch.randn(3, device=device)
        result = torch.compile(f)(x)
        self.assertEqual(result, x.sin().sin().sin())
        self.assertEqual(num_reinplacing_failures(), 0)


only_for = ("cpu", "cuda")
instantiate_device_type_tests(
    TestReinplacingPassCorrectness, globals(), only_for=only_for
)

if __name__ == "__main__":
    if HAS_CPU or HAS_GPU:
        run_tests(needs="filelock")
