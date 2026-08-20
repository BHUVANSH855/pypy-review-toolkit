"""Tests for scan_translated_divergence.py."""

import ast
import sys
import textwrap
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent
        / "plugins"
        / "pypy-review-toolkit"
        / "scripts"
    ),
)

from scan_translated_divergence import _classify_two_arm  # noqa: E402


def _body(source: str) -> list[ast.stmt]:
    return ast.parse(textwrap.dedent(source)).body


def test_translation_boundary_llop_is_not_fix():
    if_body = _body(
        """
        llop.debug_fatalerror(lltype.Void, msg)
        """
    )
    else_body = _body(
        """
        raise AssertGreenFailed(msg)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_nontranslated_helper_is_not_fix():
    if_body = _body(
        """
        raise ContinueRunningNormally(args)
        """
    )
    else_body = _body(
        """
        self._nontranslated_run_directly(args, loop_token)
        assert 0
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_debug_print_traceback_is_not_fix():
    if_body = _body(
        """
        llop.debug_print_traceback(lltype.Void)
        """
    )
    else_body = _body(
        """
        raise
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_low_level_cast_is_not_fix():
    if_body = _body(
        """
        value = lltype.cast_ptr_to_int(gcref)
        return value
        """
    )
    else_body = _body(
        """
        return id(gcref._x)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_gcref_representation_is_not_fix():
    if_body = _body(
        """
        return lltype.cast_opaque_ptr(llmemory.GCREF, x)
        """
    )
    else_body = _body(
        """
        return _GcRef(x)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_raw_storage_operation_is_not_fix():
    if_body = _body(
        """
        return raw_storage_getitem(TP, storage, index)
        """
    )
    else_body = _body(
        """
        return _raw_storage_getitem_unchecked(TP, storage, index)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_unrelated_different_calls_remain_consider():
    if_body = _body(
        """
        return translated_implementation(value)
        """
    )
    else_body = _body(
        """
        return python_implementation(value)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "different substantive functions" in reason


def test_translation_boundary_untranslated_helper_is_not_fix():
    if_body = _body(
        """
        return llmemory.cast_adr_to_int(addr)
        """
    )
    else_body = _body(
        """
        return _start_of_page_untranslated(addr, page_size)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_gil_implementation_is_not_fix():
    if_body = _body(
        """
        _gil_release()
        """
    )
    else_body = _body(
        """
        allocate()
        _emulated_gil_holder.release()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_thread_local_implementation_is_not_fix():
    if_body = _body(
        """
        return tlfield_thread_ident.getraw()
        """
    )
    else_body = _body(
        """
        return thread.get_ident()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_nonmovable_gcref_handle_is_not_fix():
    if_body = _body(
        """
        return rffi.cast(llmemory.Address, gcref)
        """
    )
    else_body = _body(
        """
        ffi = _fetch_ffi()
        x = gcref._x
        if not hasattr(x, '__handle'):
            x.__handle = ffi.new_handle(x)
        return ffi.cast("intptr_t", x.__handle)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_reveal_gcref_handle_is_not_fix():
    if_body = _body(
        """
        return rffi.cast(llmemory.GCREF, addr)
        """
    )
    else_body = _body(
        """
        ffi = _fetch_ffi()
        x = ffi.from_handle(ffi.cast("void *", addr))
        return _GcRef(x)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_finalizer_queue_is_not_fix():
    if_body = _body(
        """
        llop.gc_fq_next_dead(GCREF, tag)
        """
    )
    else_body = _body(
        """
        return self._queue.popleft()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_address_dict_emulation_is_not_fix():
    if_body = _body(
        """
        return lldict.newdict(length_estimate)
        """
    )
    else_body = _body(
        """
        return BasicAddressDict()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_address_representation_is_not_fix():
    if_body = _body(
        """
        x = rffi.cast(lltype.Signed, value)
        return x
        """
    )
    else_body = _body(
        """
        return isinstance(value, llmemory.AddressAsInt)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_unrolling_dict_lookup_is_not_fix():
    if_body = _body(
        """
        for key, value in unrolling_iteritems:
            if key == query:
                return value
        """
    )
    else_body = _body(
        """
        return d.get(query, default)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_thread_local_raw_access_is_not_fix():
    if_body = _body(
        """
        _threadlocalref_seeme(self)
        return llop.threadlocalref_get(FIELDTYPE, offset)
        """
    )
    else_body = _body(
        """
        return getattr(self.local, 'rawvalue', zero)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_thread_local_load_is_not_fix():
    if_body = _body(
        """
        _threadlocalref_seeme(self)
        return llop.threadlocalref_load(FIELDTYPE, offset)
        """
    )
    else_body = _body(
        """
        return getattr(self.local, 'rawvalue', zero)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_raw_allocation_is_not_fix():
    if_body = _body(
        """
        rawmem = lltype.malloc(rffi.CCHARP.TO, size, flavor='raw')
        return rffi.cast(CIF_DESCRIPTION_P, rawmem)
        """
    )
    else_body = _body(
        """
        return lltype.malloc(CIF_DESCRIPTION_P.TO, size, flavor='raw')
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_raw_malloc_usage_is_not_fix():
    if_body = _body(
        """
        return raw_malloc_usage(TP)
        """
    )
    else_body = _body(
        """
        return sizeof(TP)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_exception_representation_is_not_fix():
    if_body = _body(
        """
        llexception = jitexc.get_llexception(cpu, AssertionError())
        """
    )
    else_body = _body(
        """
        llexception = jitexc._get_standard_error(cpu.rtyper, AssertionError)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_dispatcher_implementation_is_not_fix():
    if_body = _body(
        """
        opnum = op.getopnum()
        for value, cls, func in ops:
            if opnum == value:
                assert isinstance(op, cls)
                return func(self, op, *args)
        if default:
            return default(self, op, *args)
        """
    )
    else_body = _body(
        """
        func = getattr(Class, name_prefix + op.getopname().upper(), None)
        if func is not None:
            return func(self, op, *args)
        if default:
            return default(self, op, *args)
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_cffi_global_var_fetch_is_not_fix():
    if_body = _body(
        """
        return pypy__cffi_fetch_var(self.fetch_addr)
        """
    )
    else_body = _body(
        """
        FNPTR = rffi.CCallback([], rffi.VOIDP)
        fetch_addr = rffi.cast(FNPTR, self.fetch_addr)
        rgil.release()
        result = fetch_addr()
        rgil.acquire()
        return result
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_interpret_exception_logging_is_not_fix():
    if_body = _body(
        """
        self._interpret()
        """
    )
    else_body = _body(
        """
        try:
            self._interpret()
        except:
            import sys
            if sys.exc_info()[0] is not None:
                self.staticdata.log(sys.exc_info()[0].__name__)
            raise
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_translation_boundary_address_name_lookup_is_not_fix():
    if_body = _body(
        """
        d = {}
        keys = self._addr2name_keys
        values = self._addr2name_values
        for i in range(len(keys)):
            d[keys[i]] = values[i]
        return d.get(addr, '')
        """
    )
    else_body = _body(
        """
        for i in range(len(self._addr2name_keys)):
            if addr == self._addr2name_keys[i]:
                return self._addr2name_values[i]
        return ''
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "CONSIDER"
    assert "translation-boundary" in reason


def test_unrecognized_control_flow_difference_remains_fix():
    if_body = _body(
        """
        return value
        """
    )
    else_body = _body(
        """
        do_something()
        """
    )

    classification, reason = _classify_two_arm(if_body, else_body)

    assert classification == "FIX"
    assert reason == "arms have inconsistent return/raise control-flow shape"
