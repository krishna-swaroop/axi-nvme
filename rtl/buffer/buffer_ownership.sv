// SPDX-License-Identifier: BSD-2-Clause
// Slot ownership for the portable capture ring.
//
// This block owns only the lifetime of slots.  The transport must not submit
// work until submit_valid/submit_ready handshakes, and recovery must be backed
// by an external DMA fence.  rst is a cold, already-quiescent reset only.
module buffer_ownership #(
    parameter integer SLOTS = 8,
    parameter integer LEASE_WIDTH = 64,
    parameter integer EPOCH_WIDTH = 32,
    parameter integer COUNTER_WIDTH = 32,
    parameter integer TIMEOUT_CYCLES = 256
) (
    input  logic clk,
    input  logic rst,

    input  logic alloc_valid,
    output logic alloc_ready,
    output logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] alloc_slot,
    output logic [LEASE_WIDTH-1:0] alloc_lease,
    output logic [EPOCH_WIDTH-1:0] epoch,

    input  logic commit_valid,
    output logic commit_ready,
    input  logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] commit_slot,
    input  logic [LEASE_WIDTH-1:0] commit_lease,
    input  logic [EPOCH_WIDTH-1:0] commit_epoch,

    input  logic submit_valid,
    output logic submit_ready,
    input  logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] submit_slot,
    input  logic [LEASE_WIDTH-1:0] submit_lease,
    input  logic [EPOCH_WIDTH-1:0] submit_epoch,

    input  logic complete_valid,
    output logic complete_ready,
    input  logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] complete_slot,
    input  logic [LEASE_WIDTH-1:0] complete_lease,
    input  logic [EPOCH_WIDTH-1:0] complete_epoch,
    input  logic complete_success,

    input  logic abort_req,
    input  logic recover_valid,
    output logic recover_ready,
    input  logic fence_done,

    output logic faulted,
    output logic error_sticky,
    output logic epoch_exhausted,
    output logic lease_exhausted,
    output logic [COUNTER_WIDTH-1:0] stale_events,
    output logic [COUNTER_WIDTH-1:0] invalid_events,
    output logic [COUNTER_WIDTH-1:0] timeout_events,
    output logic [COUNTER_WIDTH-1:0] fault_events,
    output logic counter_saturated,
    output logic [3*SLOTS-1:0] slot_states
);

    localparam integer SLOT_WIDTH = (SLOTS <= 1) ? 1 : $clog2(SLOTS);
    localparam integer TIMER_WIDTH = (TIMEOUT_CYCLES <= 2) ? 1 : $clog2(TIMEOUT_CYCLES);

    // Keep the failure visible in simulation.  The generate condition removes
    // this initial block entirely for supported configurations and synthesis.
    generate
        if (SLOTS < 1 || LEASE_WIDTH < 2 || EPOCH_WIDTH < 2 ||
            COUNTER_WIDTH < 2 || TIMEOUT_CYCLES < 2) begin : g_invalid_parameters
            initial $fatal(1, "buffer_ownership parameter constraints violated");
        end
    endgenerate

    localparam logic [2:0] FREE        = 3'd0;
    localparam logic [2:0] FILLING     = 3'd1;
    localparam logic [2:0] READY       = 3'd2;
    localparam logic [2:0] INFLIGHT    = 3'd3;
    localparam logic [2:0] QUARANTINED = 3'd4;

    localparam logic [LEASE_WIDTH-1:0] LEASE_MAX = {LEASE_WIDTH{1'b1}};
    localparam logic [EPOCH_WIDTH-1:0] EPOCH_MAX = {EPOCH_WIDTH{1'b1}};

    logic [2:0] slot_state [0:SLOTS-1];
    logic [LEASE_WIDTH-1:0] slot_lease [0:SLOTS-1];
    logic [TIMER_WIDTH-1:0] submit_age [0:SLOTS-1];
    logic [LEASE_WIDTH-1:0] next_lease;

    logic [SLOT_WIDTH-1:0] free_slot;
    logic has_free_slot;
    logic commit_stale, commit_invalid, commit_transition;
    logic submit_stale, submit_invalid, submit_transition;
    logic complete_stale, complete_invalid, complete_match;
    logic complete_error, lease_exhaust_attempt;
    logic [SLOTS-1:0] completion_match_by_slot;
    logic [SLOTS-1:0] timeout_by_slot;
    integer timeout_delta;
    integer fault_delta;
    integer stale_delta;
    integer invalid_delta;
    integer commit_slot_index;
    integer submit_slot_index;
    integer complete_slot_index;
    logic fault_now;

    function automatic [COUNTER_WIDTH-1:0] saturating_add(
        input logic [COUNTER_WIDTH-1:0] value,
        input integer amount
    );
        logic [COUNTER_WIDTH+31:0] sum;
        logic [COUNTER_WIDTH+31:0] maximum;
        begin
            if (amount <= 0) begin
                saturating_add = value;
            end else begin
                sum = {{32{1'b0}}, value};
                sum = sum + amount;
                maximum = {{32{1'b0}}, {COUNTER_WIDTH{1'b1}}};
                if (sum > maximum)
                    saturating_add = {COUNTER_WIDTH{1'b1}};
                else
                    saturating_add = sum[COUNTER_WIDTH-1:0];
            end
        end
    endfunction

    function automatic logic saturating_overflow(
        input logic [COUNTER_WIDTH-1:0] value,
        input integer amount
    );
        logic [COUNTER_WIDTH+31:0] sum;
        logic [COUNTER_WIDTH+31:0] maximum;
        begin
            sum = {{32{1'b0}}, value};
            sum = sum + amount;
            maximum = {{32{1'b0}}, {COUNTER_WIDTH{1'b1}}};
            saturating_overflow = (amount > 0) && (sum > maximum);
        end
    endfunction

    // All handle/state decisions below use the pre-edge state.  In particular,
    // a current but illegal request faults even when ready is consequently low.
    always_comb begin
        has_free_slot = 1'b0;
        free_slot = '0;
        for (integer i = 0; i < SLOTS; i = i + 1) begin
            if (!has_free_slot && slot_state[i] == FREE) begin
                has_free_slot = 1'b1;
                free_slot = i;
            end
        end

        commit_stale = 1'b0;
        commit_invalid = 1'b0;
        commit_transition = 1'b0;
        commit_slot_index = 0;
        if (commit_valid) begin
            if (commit_slot >= SLOTS) begin
                commit_invalid = 1'b1;
            end else begin
                commit_slot_index = commit_slot;
                if (faulted) begin
                    commit_stale = 1'b1;
                end else if (commit_epoch != epoch ||
                             commit_lease != slot_lease[commit_slot_index]) begin
                    commit_stale = 1'b1;
                end else if (slot_state[commit_slot_index] == FILLING) begin
                    commit_transition = 1'b1;
                end else begin
                    commit_invalid = 1'b1;
                end
            end
        end

        submit_stale = 1'b0;
        submit_invalid = 1'b0;
        submit_transition = 1'b0;
        submit_slot_index = 0;
        if (submit_valid) begin
            if (submit_slot >= SLOTS) begin
                submit_invalid = 1'b1;
            end else begin
                submit_slot_index = submit_slot;
                if (faulted) begin
                    submit_stale = 1'b1;
                end else if (submit_epoch != epoch ||
                             submit_lease != slot_lease[submit_slot_index]) begin
                    submit_stale = 1'b1;
                end else if (slot_state[submit_slot_index] == READY) begin
                    submit_transition = 1'b1;
                end else begin
                    submit_invalid = 1'b1;
                end
            end
        end

        complete_stale = 1'b0;
        complete_invalid = 1'b0;
        complete_match = 1'b0;
        complete_error = 1'b0;
        complete_slot_index = 0;
        completion_match_by_slot = '0;
        if (complete_valid) begin
            if (complete_slot >= SLOTS) begin
                complete_invalid = 1'b1;
            end else begin
                complete_slot_index = complete_slot;
                if (faulted) begin
                    complete_stale = 1'b1;
                end else if (complete_epoch != epoch ||
                             complete_lease != slot_lease[complete_slot_index]) begin
                    complete_stale = 1'b1;
                end else if (slot_state[complete_slot_index] == FREE ||
                             slot_state[complete_slot_index] == QUARANTINED) begin
                    // An exact duplicate completion for a FREE lease is stale.
                    complete_stale = 1'b1;
                end else if (slot_state[complete_slot_index] == INFLIGHT) begin
                    complete_match = 1'b1;
                    completion_match_by_slot[complete_slot_index] = 1'b1;
                    complete_error = !complete_success;
                end else begin
                    complete_invalid = 1'b1;
                end
            end
        end

        timeout_by_slot = '0;
        timeout_delta = 0;
        if (!rst && !faulted) begin
            for (integer i = 0; i < SLOTS; i = i + 1) begin
                if (slot_state[i] == INFLIGHT &&
                    submit_age[i] == TIMEOUT_CYCLES-1 &&
                    !completion_match_by_slot[i]) begin
                    timeout_by_slot[i] = 1'b1;
                    timeout_delta = 1;
                end
            end
        end

        lease_exhaust_attempt = !rst && !faulted && alloc_valid &&
                                next_lease == LEASE_MAX;
        fault_now = !rst && !faulted &&
                    (abort_req || lease_exhaust_attempt || commit_invalid ||
                     submit_invalid || complete_invalid || complete_error ||
                     timeout_delta != 0);
        // fault_events counts transitions into the faulted state, not causes.
        fault_delta = fault_now ? 1 : 0;

        alloc_slot = free_slot;
        alloc_lease = next_lease;
        alloc_ready = !rst && !faulted && !fault_now && has_free_slot &&
                      next_lease != LEASE_MAX;

        commit_ready = 1'b0;
        if (!rst) begin
            if (!faulted && !fault_now &&
                (commit_stale || commit_transition || !commit_valid))
                commit_ready = 1'b1;
        end

        submit_ready = 1'b0;
        if (!rst) begin
            if (!faulted && !fault_now &&
                (submit_stale || submit_transition || !submit_valid))
                submit_ready = 1'b1;
        end

        complete_ready = 1'b1;
        recover_ready = !rst && faulted && fence_done && epoch != EPOCH_MAX;

        stale_delta = 0;
        if (commit_valid && commit_ready && commit_stale)
            stale_delta = 1;
        if (submit_valid && submit_ready && submit_stale)
            stale_delta = 1;
        if (complete_valid && complete_ready && complete_stale)
            stale_delta = 1;

        invalid_delta = 0;
        if (commit_valid && commit_invalid)
            invalid_delta = 1;
        if (submit_valid && submit_invalid)
            invalid_delta = 1;
        if (complete_valid && complete_invalid)
            invalid_delta = 1;
    end

    always_comb begin
        for (integer i = 0; i < SLOTS; i = i + 1)
            slot_states[3*i +: 3] = slot_state[i];
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            epoch <= '0;
            next_lease <= {{(LEASE_WIDTH-1){1'b0}}, 1'b1};
            faulted <= 1'b0;
            error_sticky <= 1'b0;
            epoch_exhausted <= 1'b0;
            lease_exhausted <= 1'b0;
            counter_saturated <= 1'b0;
            stale_events <= '0;
            invalid_events <= '0;
            timeout_events <= '0;
            fault_events <= '0;
            for (integer i = 0; i < SLOTS; i = i + 1) begin
                slot_state[i] <= FREE;
                slot_lease[i] <= '0;
                submit_age[i] <= '0;
            end
        end else begin
            stale_events <= saturating_add(stale_events, stale_delta);
            invalid_events <= saturating_add(invalid_events, invalid_delta);
            timeout_events <= saturating_add(timeout_events, timeout_delta);
            fault_events <= saturating_add(fault_events, fault_delta);

            if (saturating_overflow(stale_events, stale_delta) ||
                saturating_overflow(invalid_events, invalid_delta) ||
                saturating_overflow(timeout_events, timeout_delta) ||
                saturating_overflow(fault_events, fault_delta))
                counter_saturated <= 1'b1;

            if (stale_delta != 0)
                error_sticky <= 1'b1;

            if (faulted) begin
                if (recover_valid && fence_done) begin
                    if (epoch == EPOCH_MAX) begin
                        epoch_exhausted <= 1'b1;
                        error_sticky <= 1'b1;
                    end else begin
                        epoch <= epoch + 1'b1;
                        next_lease <= {{(LEASE_WIDTH-1){1'b0}}, 1'b1};
                        faulted <= 1'b0;
                        for (integer i = 0; i < SLOTS; i = i + 1) begin
                            slot_state[i] <= FREE;
                            slot_lease[i] <= '0;
                            submit_age[i] <= '0;
                        end
                    end
                end
            end else if (fault_now) begin
                faulted <= 1'b1;
                error_sticky <= 1'b1;
                if (lease_exhaust_attempt)
                    lease_exhausted <= 1'b1;
                for (integer i = 0; i < SLOTS; i = i + 1) begin
                    if (slot_state[i] != FREE)
                        slot_state[i] <= QUARANTINED;
                    submit_age[i] <= '0;
                end
            end else begin
                if (alloc_valid && alloc_ready) begin
                    slot_state[free_slot] <= FILLING;
                    slot_lease[free_slot] <= next_lease;
                    next_lease <= next_lease + 1'b1;
                end

                if (commit_valid && commit_ready && commit_transition)
                    slot_state[commit_slot_index] <= READY;

                if (submit_valid && submit_ready && submit_transition) begin
                    slot_state[submit_slot_index] <= INFLIGHT;
                    submit_age[submit_slot_index] <= '0;
                end

                if (complete_valid && complete_ready && complete_match && complete_success) begin
                    slot_state[complete_slot_index] <= FREE;
                    submit_age[complete_slot_index] <= '0;
                end

                for (integer i = 0; i < SLOTS; i = i + 1) begin
                    if (slot_state[i] == INFLIGHT && !completion_match_by_slot[i]) begin
                        if (submit_age[i] < TIMEOUT_CYCLES-1)
                            submit_age[i] <= submit_age[i] + 1'b1;
                    end else if (slot_state[i] != INFLIGHT) begin
                        submit_age[i] <= '0;
                    end
                end
            end
        end
    end

endmodule
