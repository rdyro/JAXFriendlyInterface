import os
from subprocess import check_output
import time
import hashlib
import pickle
from warnings import warn

from .dynamic_lib_loading import load_cuda_libs

# Load OS-preferred CUDA libraries, often the ones from /usr/local/cuda ########
# unless the user decides against this #########################################
bool_map = {"0": False, "1": True, "true": True, "false": False}
if bool_map.get(os.environ.get("JAXFI_LOAD_SYSTEM_CUDA_LIBS", "0").lower(), False):
    msg = (
        "\nWe are manually loading OS preferred CUDA libraries. "
        + "This should allow JAX to work alongside PyTorch "
        + "(which currently bundles different-version CUDA libraries). "
        + "Set JAXFI_LOAD_SYSTEM_CUDA_LIBS=0 to disable this behavior."
    )
    warn(msg)
    load_cuda_libs()
################################################################################

from . import globals
from .utils import copy_module, default_dtype_for_device, _is_dtype, resolve_device
from .utils import manual_seed, make_random_keys, make_random_key
from .utils import get_default_device, get_default_dtype, set_default_device, set_default_dtype
from .utils import _enable_pickling_fixes
from .utils import _tree_jaxm_to


def init(seed=None):
    """Initializes the wrapped jax backend setting the platform (e.g., GPU) and random seed."""

    os.environ.setdefault("JAX_ENABLE_X64", "True")
    os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", str(False))
    os.environ.setdefault("XLA_PYTHON_CLIENT_ALLOCATOR", "platform")
    existing_xla_flags = os.environ.get("XLA_FLAGS", "")
    os.environ["XLA_FLAGS"] = (
        existing_xla_flags + f" --xla_force_host_platform_device_count={os.cpu_count()}"
    )
    if os.environ.get("JAX_PLATFORM_NAME") is None:
        try:
            check_output("nvidia-smi")
            gpu_available = True
        except FileNotFoundError:
            gpu_available = False
        os.environ["JAX_PLATFORM_NAME"] = "GPU" if gpu_available else "CPU"

    import jax
    import jax.numpy as jnp
    import jax.random as jrandom
    import jax.scipy as jsp

    globals.jax, globals.jnp, globals.jrandom, globals.jsp = jax, jnp, jrandom, jsp

    try:
        globals.jax.config.update("jax_enable_x64", True)  # ensure float64 support
    except AttributeError:
        pass

    # binding main derivatives and jit
    jaxm = copy_module(jnp, recursive=False)

    jaxm.grad, jaxm.jacobian, jaxm.hessian = jax.grad, jax.jacobian, jax.hessian
    jaxm.jvp, jaxm.vjp, jaxm.stop_gradient = jax.jvp, jax.vjp, jax.lax.stop_gradient
    jaxm.jit, jaxm.vmap = jax.jit, jax.vmap

    DEFAULT_DEVICE, DEFAULT_DTYPE = jax.devices("cpu")[0], jnp.float32
    globals.DEFAULT_DEVICE, globals.DEFAULT_DTYPE = DEFAULT_DEVICE, DEFAULT_DTYPE
    try:
        globals.jax.config.update("jax_default_device", DEFAULT_DEVICE)
    except AttributeError:
        pass

    # binding random numbers
    seed = None
    seed = (
        seed
        if seed is not None
        else hash(
            hashlib.sha256(pickle.dumps((time.time(), os.getpid(), os.urandom(100)))).hexdigest()
        )
    )
    #key = jax.device_put(jrandom.PRNGKey(seed), jax.devices("cpu")[0])
    key = jrandom.PRNGKey(seed)
    globals.key = key

    def device_dtype_fn(fn, without_dtype=False, check_second_arg_for_dtype=False):
        def fn_(*args, **kw):
            device = resolve_device(kw.get("device", None))
            if "device" in kw:
                del kw["device"]
            if not without_dtype:
                kw["dtype"] = kw.get("dtype", default_dtype_for_device(device))
            if check_second_arg_for_dtype and len(args) >= 2 and _is_dtype(args[1]):
                kw["dtype"] = args[1]
                args = args[:1] + args[2:]
            return fn(*args, **kw) if device is None else jax.device_put(fn(*args, **kw), device)

        return fn_

    jaxm.array = device_dtype_fn(jnp.array, check_second_arg_for_dtype=True)
    jaxm.ones = device_dtype_fn(jnp.ones, check_second_arg_for_dtype=True)
    jaxm.zeros = device_dtype_fn(jnp.zeros, check_second_arg_for_dtype=True)
    jaxm.full = device_dtype_fn(jnp.full, check_second_arg_for_dtype=True)
    jaxm.eye = device_dtype_fn(jnp.eye)
    jaxm.arange = device_dtype_fn(jnp.arange, without_dtype=True)
    jaxm.linspace = device_dtype_fn(jnp.linspace)
    jaxm.logspace = device_dtype_fn(jnp.logspace)

    def random_fn(fn, first_arg_to_tuple=False, default_dtype=None):
        def jaxm_fn(*args, **kw):
            if "key" in kw and kw["key"] is None:
                kw = {k: v for (k, v) in kw.items() if k != "key"}
            if "key" in kw:
                key1 = kw["key"]  # use user provided key
                kw = {k: v for (k, v) in kw.items() if k != "key"}
            else:
                key1, key2 = jrandom.split(globals.key)
                if not isinstance(key2, jax.interpreters.partial_eval.DynamicJaxprTracer):
                    globals.key = key2
            # set correct device and dtype
            key1, key2 = jrandom.split(globals.key)
            # under_jit = isinstance(key2, jax.interpreters.partial_eval.DynamicJaxprTracer)
            # default_device = None if under_jit else get_default_device()
            default_device = None
            device = resolve_device(kw.get("device", default_device))
            if "device" in kw:
                del kw["device"]
            ddtype = (
                default_dtype if default_dtype is not None else default_dtype_for_device(device)
            )
            kw["dtype"] = kw.get("dtype", ddtype)
            # make the size a tuple if this is the only positional argument
            if first_arg_to_tuple and len(args) == 1 and not hasattr(args[0], "__iter__"):
                args = [(args[0],)] + list(args)[1:]
            ret = (
                fn(key1, *args, **kw)
                if device is None
                else jax.device_put(fn(key1, *args, **kw), device)
            )
            return ret

        return jaxm_fn

    jaxm.randn = random_fn(jrandom.normal, first_arg_to_tuple=True)
    jaxm.rand = random_fn(jrandom.uniform, first_arg_to_tuple=True)
    jaxm.randint = random_fn(
        lambda key, low, high, size, dtype=None: jrandom.randint(key, size, low, high, dtype=dtype),
        default_dtype=jaxm.int64,
    )
    jaxm.randperm = lambda n, device=None, dtype=None: jaxm.argsort(
        jaxm.rand(n, device=device, dtype=dtype)
    )

    # LA factorizations and solves
    jaxm.linalg.cholesky = jsp.linalg.cho_factor
    jaxm.linalg.cholesky_solve = jsp.linalg.cho_solve
    jaxm.linalg.lu_factor = jsp.linalg.lu_factor
    jaxm.linalg.lu_solve = jsp.linalg.lu_solve

    # some utility bindings
    jaxm.norm = jnp.linalg.norm
    jaxm.softmax = jax.nn.softmax
    jaxm.cat = jnp.concatenate
    jaxm.t = lambda x: jaxm.swapaxes(x, -1, -2)
    jaxm.nn = jax.nn
    jaxm.manual_seed = manual_seed
    jaxm.get_default_dtype = get_default_dtype
    jaxm.set_default_dtype = set_default_dtype
    jaxm.get_default_device = get_default_device
    jaxm.set_default_device = set_default_device
    jaxm.default_dtype_for_device = default_dtype_for_device
    jaxm.resolve_device = resolve_device
    jaxm.make_random_keys = make_random_keys
    jaxm.make_random_key = make_random_key
    jaxm.to = _tree_jaxm_to

    # some control flow bindings
    jaxm.cond = jax.lax.cond
    jaxm.scan = jax.lax.scan
    jaxm.while_loop = jax.lax.while_loop
    jaxm.fori_loop = jax.lax.fori_loop

    # module bindings
    jaxm.jax = jax
    jaxm.numpy = jnp
    jaxm.lax = jax.lax
    jaxm.scipy = jsp
    jaxm.random = jrandom

    _enable_pickling_fixes()
    return jaxm


####################################################################################################
jaxm = init()
globals.jaxm = jaxm
