"""Independent handle/state oracle for the portable slot owner."""

import random
import os

import cocotb
from cocotb.triggers import Timer


FREE, FILLING, READY, INFLIGHT, QUARANTINED = range(5)
STATE_NAMES = ("FREE", "FILLING", "READY", "INFLIGHT", "QUARANTINED")
TIMEOUT_CYCLES = int(os.environ.get("BUFFER_TEST_TIMEOUT_CYCLES", "4"))


class OwnershipOracle:
    """Cycle-level contract model; it never reads DUT-private state."""

    def __init__(self, dut):
        self.slots = len(dut.slot_states) // 3
        self.lease_width = len(dut.alloc_lease)
        self.epoch_width = len(dut.epoch)
        self.counter_width = len(dut.stale_events)
        self.lease_max = (1 << self.lease_width) - 1
        self.epoch_max = (1 << self.epoch_width) - 1
        self.counter_max = (1 << self.counter_width) - 1
        self.reset()

    def reset(self):
        self.states = [FREE] * self.slots
        self.leases = [0] * self.slots
        self.ages = [0] * self.slots
        self.epoch = 0
        self.next_lease = 1
        self.faulted = False
        self.error_sticky = False
        self.epoch_exhausted = False
        self.lease_exhausted = False
        self.counter_saturated = False
        self.counts = {"stale_events": 0, "invalid_events": 0,
                       "timeout_events": 0, "fault_events": 0}

    def _classify(self, values, prefix, expected_state=None):
        if not values[f"{prefix}_valid"]:
            return "none", None
        slot = values[f"{prefix}_slot"]
        if slot >= self.slots:
            return "invalid", None
        if self.faulted:
            return "stale", slot
        if values[f"{prefix}_epoch"] != self.epoch or values[f"{prefix}_lease"] != self.leases[slot]:
            return "stale", slot
        state = self.states[slot]
        if expected_state is not None:
            return ("transition", slot) if state == expected_state else ("invalid", slot)
        if state in (FREE, QUARANTINED):
            return "stale", slot
        if state == INFLIGHT:
            return "match", slot
        return "invalid", slot

    def preview(self, values):
        has_free = any(state == FREE for state in self.states)
        commit_kind, commit_slot = self._classify(values, "commit", FILLING)
        submit_kind, submit_slot = self._classify(values, "submit", READY)
        complete_kind, complete_slot = self._classify(values, "complete")
        complete_matches = set()
        if complete_kind == "match":
            complete_matches.add(complete_slot)

        timeout_slots = set()
        if not self.faulted and not values["rst"]:
            timeout_slots = {
                i for i, state in enumerate(self.states)
                if state == INFLIGHT and self.ages[i] == TIMEOUT_CYCLES - 1
                and i not in complete_matches
            }

        lease_exhaust_attempt = (not self.faulted and not values["rst"]
                                 and values["alloc_valid"] and self.next_lease == self.lease_max)
        invalid_any = "invalid" in (commit_kind, submit_kind, complete_kind)
        completion_error = (complete_kind == "match" and not values["complete_success"])
        fault_now = (not self.faulted and not values["rst"] and
                     (values["abort_req"] or lease_exhaust_attempt or invalid_any or
                      completion_error or bool(timeout_slots)))

        alloc_ready = (not values["rst"] and not self.faulted and not fault_now
                       and has_free and self.next_lease != self.lease_max)
        if values["rst"]:
            commit_ready = submit_ready = False
        elif self.faulted:
            commit_ready = submit_ready = False
        else:
            commit_ready = not fault_now and (not values["commit_valid"] or
                                               commit_kind in ("stale", "transition"))
            submit_ready = not fault_now and (not values["submit_valid"] or
                                               submit_kind in ("stale", "transition"))

        recover_ready = (not values["rst"] and self.faulted and values["fence_done"]
                         and self.epoch != self.epoch_max)
        return {
            "alloc_ready": int(alloc_ready),
            "alloc_lease": self.next_lease, "commit_ready": int(commit_ready),
            "submit_ready": int(submit_ready), "complete_ready": 1,
            "recover_ready": int(recover_ready), "fault_now": fault_now,
            "lease_exhaust_attempt": lease_exhaust_attempt,
            "commit_kind": commit_kind, "commit_slot": commit_slot,
            "submit_kind": submit_kind, "submit_slot": submit_slot,
            "complete_kind": complete_kind, "complete_slot": complete_slot,
            "complete_matches": complete_matches, "timeout_slots": timeout_slots,
        }

    def _count_cycle(self, deltas):
        for name, delta in deltas.items():
            if delta and self.counts[name] == self.counter_max:
                self.counter_saturated = True
            elif delta and self.counts[name] + delta > self.counter_max:
                self.counter_saturated = True
            self.counts[name] = min(self.counter_max, self.counts[name] + delta)

    def advance(self, values, view):
        if values["rst"]:
            self.reset()
            return

        stale = ((values["commit_valid"] and view["commit_kind"] == "stale"
                  and view["commit_ready"]) or
                 (values["submit_valid"] and view["submit_kind"] == "stale"
                  and view["submit_ready"]) or
                 (values["complete_valid"] and view["complete_kind"] == "stale"))
        invalid = any(values[f"{name}_valid"] and view[f"{name}_kind"] == "invalid"
                      for name in ("commit", "submit", "complete"))
        timeout = bool(view["timeout_slots"])
        self._count_cycle({
            "stale_events": int(bool(stale)),
            "invalid_events": int(bool(invalid)),
            "timeout_events": int(timeout),
            "fault_events": int(view["fault_now"]),
        })
        if stale:
            self.error_sticky = True

        if self.faulted:
            if values["recover_valid"] and values["fence_done"]:
                if self.epoch == self.epoch_max:
                    self.epoch_exhausted = True
                    self.error_sticky = True
                else:
                    self.epoch += 1
                    self.next_lease = 1
                    self.faulted = False
                    self.states = [FREE] * self.slots
                    self.leases = [0] * self.slots
                    self.ages = [0] * self.slots
            return

        if view["fault_now"]:
            self.faulted = True
            self.error_sticky = True
            if view["lease_exhaust_attempt"]:
                self.lease_exhausted = True
            self.states = [FREE if state == FREE else QUARANTINED for state in self.states]
            self.ages = [0] * self.slots
            return

        old_states = list(self.states)
        old_ages = list(self.ages)
        if values["alloc_valid"] and view["alloc_ready"]:
            slot = view["alloc_slot"]
            self.states[slot] = FILLING
            self.leases[slot] = self.next_lease
            self.next_lease += 1
        if (values["commit_valid"] and view["commit_ready"]
                and view["commit_kind"] == "transition"):
            self.states[view["commit_slot"]] = READY
        if (values["submit_valid"] and view["submit_ready"]
                and view["submit_kind"] == "transition"):
            slot = view["submit_slot"]
            self.states[slot] = INFLIGHT
            self.ages[slot] = 0
        if (values["complete_valid"] and view["complete_kind"] == "match"
                and values["complete_success"]):
            slot = view["complete_slot"]
            self.states[slot] = FREE
            self.ages[slot] = 0

        for slot, state in enumerate(old_states):
            if state == INFLIGHT and slot not in view["complete_matches"]:
                if old_ages[slot] < TIMEOUT_CYCLES - 1:
                    self.ages[slot] = old_ages[slot] + 1
            elif state != INFLIGHT:
                self.ages[slot] = 0

    def check(self, dut):
        packed = sum(state << (3 * i) for i, state in enumerate(self.states))
        assert int(dut.slot_states.value) == packed, (
            f"states got {int(dut.slot_states.value):x}, expected {packed:x} "
            f"({[STATE_NAMES[s] for s in self.states]})"
        )
        assert int(dut.epoch.value) == self.epoch
        assert int(dut.faulted.value) == self.faulted
        assert int(dut.error_sticky.value) == self.error_sticky
        assert int(dut.epoch_exhausted.value) == self.epoch_exhausted
        assert int(dut.lease_exhausted.value) == self.lease_exhausted
        assert int(dut.counter_saturated.value) == self.counter_saturated
        for name, value in self.counts.items():
            assert int(getattr(dut, name).value) == value, f"{name}: got {int(getattr(dut, name).value)}, expected {value}"


class Bench:
    INPUTS = (
        "alloc_valid", "commit_valid", "commit_slot", "commit_lease", "commit_epoch",
        "submit_valid", "submit_slot", "submit_lease", "submit_epoch",
        "complete_valid", "complete_slot", "complete_lease", "complete_epoch",
        "complete_success", "abort_req", "recover_valid", "fence_done",
    )

    def __init__(self, dut):
        self.dut = dut
        self.oracle = OwnershipOracle(dut)
        self.random = random

    def _values(self, overrides):
        values = {name: 0 for name in self.INPUTS}
        values.update(overrides)
        values["rst"] = int(overrides.get("rst", 0))
        return values

    async def cycle(self, **overrides):
        values = self._values(overrides)
        d = self.dut
        d.clk.value = 0
        for name, value in values.items():
            getattr(d, name).value = value
        d.rst.value = values["rst"]
        await Timer(5, unit="ns")

        view = self.oracle.preview(values)
        for name in ("alloc_ready", "commit_ready", "submit_ready", "complete_ready", "recover_ready"):
            got = int(getattr(d, name).value)
            assert got == view[name], f"{name}={got}, expected {view[name]} for {values}"
        if self.oracle.faulted:
            assert int(d.alloc_ready.value) == 0
            assert int(d.commit_ready.value) == 0
            assert int(d.submit_ready.value) == 0
        # The interface permits any FREE slot to be granted. Inspect payload
        # only on a real handshake and feed that observed grant to the oracle.
        if values["alloc_valid"] and view["alloc_ready"]:
            granted_slot = int(d.alloc_slot.value)
            granted_lease = int(d.alloc_lease.value)
            assert 0 <= granted_slot < self.oracle.slots
            assert self.oracle.states[granted_slot] == FREE, (
                f"allocator granted owned slot {granted_slot}: {self.oracle.states}"
            )
            assert granted_lease == self.oracle.next_lease
            view["alloc_slot"] = granted_slot
            view["alloc_lease"] = granted_lease

        self.oracle.advance(values, view)
        d.clk.value = 1
        await Timer(5, unit="ns")
        self.oracle.check(d)
        return view

    async def reset(self):
        for _ in range(3):
            await self.cycle(rst=1)
        await self.cycle()

    async def alloc(self):
        view = await self.cycle(alloc_valid=1)
        if not view["alloc_ready"]:
            return None
        return {"slot": view["alloc_slot"], "lease": view["alloc_lease"],
                "epoch": self.oracle.epoch}

    async def commit(self, handle):
        return await self.cycle(commit_valid=1, commit_slot=handle["slot"],
                                commit_lease=handle["lease"], commit_epoch=handle["epoch"])

    async def submit(self, handle):
        return await self.cycle(submit_valid=1, submit_slot=handle["slot"],
                                submit_lease=handle["lease"], submit_epoch=handle["epoch"])

    async def complete(self, handle, success=True):
        return await self.cycle(complete_valid=1, complete_slot=handle["slot"],
                                complete_lease=handle["lease"], complete_epoch=handle["epoch"],
                                complete_success=int(success))

    async def recover(self, *, fence=True):
        return await self.cycle(recover_valid=1, fence_done=int(fence))


@cocotb.test()
async def reorder_duplicate_and_old_epoch_handles(dut):
    b = Bench(dut)
    await b.reset()
    first = await b.alloc()
    second = await b.alloc()
    assert first is not None

    if b.oracle.slots >= 2:
        assert second is not None and second["slot"] != first["slot"]
        await b.commit(first)
        await b.commit(second)
        await b.submit(first)
        await b.submit(second)
        for _ in range(random.randrange(2)):
            await b.cycle()  # seed-controlled transport delay without reaching the deadline
        await b.complete(second)
        assert b.oracle.states[second["slot"]] == FREE
        # Retire the other request on its deadline if the seed inserted one
        # idle delay above; this keeps the short timeout from obscuring the
        # ownership checks that follow.
        await b.complete(first)
        assert b.oracle.states[first["slot"]] == FREE
        await b.complete(second)  # current-token duplicate after FREE is stale

        # Consume any grants needed to reach the freed slot; the interface
        # allows every free-slot selection policy.
        reused = None
        for _ in range(b.oracle.slots):
            granted = await b.alloc()
            assert granted is not None
            if granted["slot"] == second["slot"]:
                reused = granted
                break
        assert reused is not None
        assert reused["lease"] != second["lease"]
        await b.complete(second)  # stale old lease cannot release the new allocation
        assert b.oracle.states[reused["slot"]] == FILLING
        await b.commit(reused)
        await b.submit(reused)
    else:
        second = first
        await b.commit(first)
        await b.submit(first)
        await b.complete(first)
        await b.complete(first)
        reused = await b.alloc()
        assert reused is not None and reused["lease"] != first["lease"]
        await b.complete(first)
        assert b.oracle.states[reused["slot"]] == FILLING

    await b.cycle(abort_req=1)
    assert b.oracle.faulted
    assert all(state in (FREE, QUARANTINED) for state in b.oracle.states)
    before_epoch = b.oracle.epoch
    await b.recover(fence=False)
    assert b.oracle.faulted and b.oracle.epoch == before_epoch
    # Even a valid recovery handshake cannot authorize new work on its
    # own pre-edge faulted cycle. The owner accepts it on the next cycle.
    view = await b.cycle(
        alloc_valid=1,
        commit_valid=1, commit_slot=reused["slot"],
        commit_lease=reused["lease"], commit_epoch=reused["epoch"],
        submit_valid=1, submit_slot=reused["slot"],
        submit_lease=reused["lease"], submit_epoch=reused["epoch"],
        recover_valid=1, fence_done=1,
    )
    assert view["recover_ready"] == 1
    assert view["alloc_ready"] == view["commit_ready"] == view["submit_ready"] == 0
    assert not b.oracle.faulted and b.oracle.epoch == before_epoch + 1
    new_handle = await b.alloc()
    assert new_handle is not None and new_handle["epoch"] == before_epoch + 1

    assert new_handle["lease"] == 1
    old_handle = first if b.oracle.slots == 1 else reused
    await b.complete(old_handle)  # old epoch is stale even if slot/lease match
    assert b.oracle.states[new_handle["slot"]] == FILLING


@cocotb.test()
async def abort_quarantines_filling_ready_and_inflight(dut):
    b = Bench(dut)
    await b.reset()

    # Each case starts from a fresh cold reset so SLOTS=1 also covers all states.
    for stage in ("filling", "ready", "inflight"):
        if stage != "filling":
            await b.reset()
        handle = await b.alloc()
        assert handle is not None
        if stage in ("ready", "inflight"):
            await b.commit(handle)
        if stage == "inflight":
            await b.submit(handle)

        await b.cycle(abort_req=1)
        assert b.oracle.states[handle["slot"]] == QUARANTINED
        assert b.oracle.faulted
        stale_before_late_completion = b.oracle.counts["stale_events"]
        late = await b.complete(handle)
        assert late["complete_ready"] == 1
        assert b.oracle.counts["stale_events"] == stale_before_late_completion + 1
        assert b.oracle.states[handle["slot"]] == QUARANTINED
        # No DMA fence means no slot may be reused or cleared.
        await b.recover(fence=False)
        assert b.oracle.states[handle["slot"]] == QUARANTINED
        assert b.oracle.faulted
        await b.recover(fence=True)
        assert b.oracle.states[handle["slot"]] == FREE
        assert b.oracle.epoch == handle["epoch"] + 1


@cocotb.test()
async def completion_wins_its_deadline_but_other_slot_can_timeout(dut):
    b = Bench(dut)
    await b.reset()
    first = await b.alloc()
    await b.commit(first)
    second = None
    if b.oracle.slots >= 2:
        second = await b.alloc()
        await b.commit(second)
    await b.submit(first)

    if b.oracle.slots >= 2:
        assert second is not None
        for _ in range(TIMEOUT_CYCLES - 1):
            await b.cycle()
            assert not b.oracle.faulted
        await b.complete(first)  # on its deadline; this slot's timeout is suppressed
        assert not b.oracle.faulted
        assert b.oracle.states[first["slot"]] == FREE

        # Make another slot time out while a matching completion for a
        # different, younger slot arrives. The successful CQE cannot mask the
        # peer's deadline.
        third = await b.alloc()
        await b.commit(third)
        await b.submit(second)
        for _ in range(TIMEOUT_CYCLES - 2):
            await b.cycle()
            assert not b.oracle.faulted
        await b.submit(third)
        await b.complete(third)
        assert b.oracle.faulted
        assert b.oracle.counts["timeout_events"] == 1
        assert b.oracle.states[second["slot"]] == QUARANTINED
        assert b.oracle.states[third["slot"]] == QUARANTINED
    else:
        for _ in range(TIMEOUT_CYCLES - 1):
            await b.cycle()
            assert not b.oracle.faulted
        await b.complete(first)  # completion on deadline suppresses timeout
        assert not b.oracle.faulted
        assert b.oracle.states[first["slot"]] == FREE

        second = await b.alloc()
        await b.commit(second)
        await b.submit(second)
        for _ in range(TIMEOUT_CYCLES):
            await b.cycle()
        assert b.oracle.faulted
        assert b.oracle.counts["timeout_events"] == 1
        assert b.oracle.states[second["slot"]] == QUARANTINED


@cocotb.test()
async def simultaneous_distinct_transitions_and_same_slot_conflict(dut):
    b = Bench(dut)
    await b.reset()
    first = await b.alloc()

    if b.oracle.slots >= 3:
        second = await b.alloc()
        third = await b.alloc()
        await b.commit(second)
        # Prepare three distinct pre-edge states: FILLING, READY, INFLIGHT.
        # The third allocation is still FILLING, so finish it on earlier edges.
        await b.commit(third)
        await b.submit(third)
        fourth = await b.alloc() if b.oracle.slots >= 4 else first
        # For SLOTS=3, first remains FILLING and serves the commit event.
        # For SLOTS>=4, use fourth as a fresh FILLING slot instead.
        fill_handle = first if b.oracle.slots == 3 else fourth
        await b.cycle(
            commit_valid=1, commit_slot=fill_handle["slot"],
            commit_lease=fill_handle["lease"], commit_epoch=fill_handle["epoch"],
            submit_valid=1, submit_slot=second["slot"],
            submit_lease=second["lease"], submit_epoch=second["epoch"],
            complete_valid=1, complete_slot=third["slot"],
            complete_lease=third["lease"], complete_epoch=third["epoch"],
            complete_success=1,
        )
        assert not b.oracle.faulted
        assert b.oracle.states[fill_handle["slot"]] == READY
        assert b.oracle.states[second["slot"]] == INFLIGHT
        assert b.oracle.states[third["slot"]] == FREE

    # Both events name the same current handle but target incompatible
    # pre-edge states; a fault must block both handshakes and quarantine all.
    conflict = first if b.oracle.slots == 1 else await b.alloc()
    assert conflict is not None
    await b.cycle(
        commit_valid=1, commit_slot=conflict["slot"],
        commit_lease=conflict["lease"], commit_epoch=conflict["epoch"],
        submit_valid=1, submit_slot=conflict["slot"],
        submit_lease=conflict["lease"], submit_epoch=conflict["epoch"],
    )
    assert b.oracle.faulted
    assert b.oracle.states[conflict["slot"]] == QUARANTINED


@cocotb.test()
async def failed_completion_quarantines_owned_slots_and_blocks_new_work(dut):
    b = Bench(dut)
    await b.reset()

    failed = await b.alloc()
    assert failed is not None
    await b.commit(failed)
    await b.submit(failed)

    if b.oracle.slots >= 3:
        filling = await b.alloc()
        ready = await b.alloc()
        assert filling is not None and ready is not None
        await b.commit(ready)

        # On the failing-CQE edge, otherwise-valid allocation, commit and
        # submit events are all suppressed. Every previously owned slot is
        # quarantined; any remaining FREE slot stays FREE.
        states_before_error = list(b.oracle.states)
        view = await b.cycle(
            alloc_valid=1,
            commit_valid=1, commit_slot=filling["slot"],
            commit_lease=filling["lease"], commit_epoch=filling["epoch"],
            submit_valid=1, submit_slot=ready["slot"],
            submit_lease=ready["lease"], submit_epoch=ready["epoch"],
            complete_valid=1, complete_slot=failed["slot"],
            complete_lease=failed["lease"], complete_epoch=failed["epoch"],
            complete_success=0,
        )
        assert view["alloc_ready"] == view["commit_ready"] == view["submit_ready"] == 0
        assert b.oracle.states[failed["slot"]] == QUARANTINED
        assert b.oracle.states[filling["slot"]] == QUARANTINED
        assert b.oracle.states[ready["slot"]] == QUARANTINED
        assert b.oracle.states == [
            FREE if state == FREE else QUARANTINED for state in states_before_error
        ]
    else:
        # SLOTS=1 has no free slot or distinct current handles for simultaneous
        # transitions. Still exercise error completion alongside both
        # wrong-state current requests and an allocation request.
        view = await b.cycle(
            alloc_valid=1,
            commit_valid=1, commit_slot=failed["slot"],
            commit_lease=failed["lease"], commit_epoch=failed["epoch"],
            submit_valid=1, submit_slot=failed["slot"],
            submit_lease=failed["lease"], submit_epoch=failed["epoch"],
            complete_valid=1, complete_slot=failed["slot"],
            complete_lease=failed["lease"], complete_epoch=failed["epoch"],
            complete_success=0,
        )
        assert view["alloc_ready"] == view["commit_ready"] == view["submit_ready"] == 0
        assert b.oracle.states[failed["slot"]] == QUARANTINED

    assert b.oracle.faulted
    assert b.oracle.counts["fault_events"] == 1
    assert b.oracle.error_sticky
    await b.recover(fence=True)
    clean = await b.alloc()
    assert clean is not None and clean["epoch"] == b.oracle.epoch


@cocotb.test()
async def invalid_slot_and_wrong_state_handles_fault(dut):
    b = Bench(dut)
    await b.reset()

    # A current matching handle in the wrong pre-edge state is a protocol
    # error. Check commit from READY and submit from FILLING independently.
    handle = await b.alloc()
    assert handle is not None
    await b.commit(handle)
    await b.commit(handle)
    assert b.oracle.faulted and b.oracle.counts["invalid_events"] == 1
    await b.recover(fence=True)

    handle = await b.alloc()
    assert handle is not None
    await b.submit(handle)
    assert b.oracle.faulted and b.oracle.counts["invalid_events"] == 2
    await b.recover(fence=True)

    slot_width = len(b.dut.complete_slot)
    if (1 << slot_width) > b.oracle.slots:
        # Non-power-of-two configurations have representable out-of-range
        # indices. The completion input is always drainable but must never be
        # used to index slot storage.
        await b.cycle(complete_valid=1, complete_slot=b.oracle.slots,
                      complete_success=1)
        assert b.oracle.faulted and b.oracle.counts["invalid_events"] == 3
        assert all(state == FREE for state in b.oracle.states)


@cocotb.test()
async def tiny_width_exhaustion_sticky_counters_and_recovery(dut):
    b = Bench(dut)
    await b.reset()

    if b.oracle.lease_width == 64:
        # Nominal-width profile: exercise several more unique leases than
        # there are slots, then prove error/counter history survives recovery.
        assert b.oracle.lease_width == 64
        assert b.oracle.epoch_width == 32
        assert b.oracle.counter_width == 32
        await b.cycle(complete_valid=1, complete_slot=0, complete_lease=1,
                      complete_epoch=1, complete_success=1)
        assert b.oracle.error_sticky and b.oracle.counts["stale_events"] == 1
        assert not b.oracle.counter_saturated
        seen = set()
        for _ in range(3 * b.oracle.slots + 1):
            handle = await b.alloc()
            assert handle is not None and handle["lease"] not in seen
            seen.add(handle["lease"])
            await b.commit(handle)
            await b.submit(handle)
            await b.complete(handle)
        assert len(seen) == 3 * b.oracle.slots + 1
        assert not b.oracle.lease_exhausted and not b.oracle.faulted
        old_epoch = b.oracle.epoch
        await b.cycle(abort_req=1)
        await b.recover(fence=True)
        assert b.oracle.epoch == old_epoch + 1 and not b.oracle.faulted
        assert b.oracle.error_sticky and b.oracle.counts["stale_events"] == 1
        assert not b.oracle.counter_saturated and not b.oracle.lease_exhausted
        restarted = await b.alloc()
        assert restarted is not None and restarted["lease"] == 1
        return

    assert b.oracle.lease_width in (3, 4)
    assert b.oracle.epoch_width == 2
    assert b.oracle.counter_width == 2

    # Stale events count once per cycle, saturate, and do not fault healthy work.
    for _ in range(b.oracle.counter_max + 1):
        await b.cycle(complete_valid=1, complete_slot=0, complete_lease=1,
                      complete_epoch=1, complete_success=1)
    assert not b.oracle.faulted
    assert b.oracle.counts["stale_events"] == b.oracle.counter_max
    assert b.oracle.counter_saturated
    assert b.oracle.error_sticky

    # Lease MAX is reserved.  The preceding leases are usable and released.
    for expected_lease in range(1, b.oracle.lease_max):
        handle = await b.alloc()
        assert handle is not None and handle["lease"] == expected_lease
        await b.commit(handle)
        await b.submit(handle)
        await b.complete(handle)
    assert b.oracle.next_lease == b.oracle.lease_max
    refused = await b.alloc()
    assert refused is None
    assert b.oracle.faulted and b.oracle.lease_exhausted
    assert all(state == FREE for state in b.oracle.states)
    old_epoch = b.oracle.epoch
    await b.recover(fence=True)
    assert b.oracle.epoch == old_epoch + 1 and not b.oracle.faulted
    assert b.oracle.lease_exhausted  # historical sticky status does not block allocation
    restarted = await b.alloc()
    assert restarted is not None and restarted["lease"] == 1
    await b.cycle(abort_req=1)
    await b.recover(fence=True)
    await b.cycle(abort_req=1)
    await b.recover(fence=True)
    assert b.oracle.epoch == b.oracle.epoch_max
    await b.cycle(abort_req=1)
    assert b.oracle.faulted
    await b.recover(fence=True)
    assert b.oracle.faulted and b.oracle.epoch_exhausted
    assert b.oracle.error_sticky and b.oracle.counter_saturated


@cocotb.test()
async def seeded_random_valid_ownership_cycles(dut):
    b = Bench(dut)
    await b.reset()
    rng = random.Random(random.getrandbits(64))
    handles = {}
    leases = set()
    slot_allocations = [0] * b.oracle.slots

    for _ in range(400):
        filling = [i for i, state in enumerate(b.oracle.states) if state == FILLING]
        ready = [i for i, state in enumerate(b.oracle.states) if state == READY]
        inflight = [i for i, state in enumerate(b.oracle.states) if state == INFLIGHT]
        urgent = [i for i in inflight if b.oracle.ages[i] >= TIMEOUT_CYCLES - 2]

        if urgent:
            action, slot = "complete", max(urgent, key=lambda i: b.oracle.ages[i])
        else:
            actions = []
            if (any(state == FREE for state in b.oracle.states)
                    and b.oracle.next_lease != b.oracle.lease_max):
                actions.extend(("alloc",) * 4)
            if filling:
                actions.extend(("commit",) * 3)
            if ready:
                actions.extend(("submit",) * 3)
            if inflight:
                actions.extend(("complete",) * 4)
            action = rng.choice(actions) if actions else "idle"
            slot = None

        if action == "alloc":
            handle = await b.alloc()
            if handle is not None:
                assert handle["slot"] not in handles
                assert handle["lease"] not in leases
                handles[handle["slot"]] = handle
                leases.add(handle["lease"])
                slot_allocations[handle["slot"]] += 1
        elif action == "commit":
            slot = rng.choice(filling)
            await b.commit(handles[slot])
        elif action == "submit":
            slot = rng.choice(ready)
            await b.submit(handles[slot])
        elif action == "complete":
            if slot is None:
                slot = rng.choice(inflight)
            await b.complete(handles[slot])
            del handles[slot]
        else:
            await b.cycle()

        assert not b.oracle.faulted

    # Drain all valid work with no more than one in-flight completion deadline
    # approaching at once, then require actual reuse in the nominal profile.
    while any(state != FREE for state in b.oracle.states):
        filling = [i for i, state in enumerate(b.oracle.states) if state == FILLING]
        ready = [i for i, state in enumerate(b.oracle.states) if state == READY]
        inflight = [i for i, state in enumerate(b.oracle.states) if state == INFLIGHT]
        if inflight:
            slot = max(inflight, key=lambda i: b.oracle.ages[i])
            await b.complete(handles[slot])
            del handles[slot]
        elif ready:
            slot = rng.choice(ready)
            await b.submit(handles[slot])
        elif filling:
            slot = rng.choice(filling)
            await b.commit(handles[slot])
        assert not b.oracle.faulted

    if b.oracle.lease_width == 64:
        assert len(leases) >= 3 * b.oracle.slots
        assert max(slot_allocations) > 1
