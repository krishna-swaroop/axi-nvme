// SPDX-License-Identifier: BSD-2-Clause
// Portable, non-pausable fixed-chunk capture staging.
module capture_stager #(
    parameter integer DATA_WIDTH      = 256,
    parameter integer CHUNK_BEATS     = 128,
    parameter integer BANKS           = 4,
    parameter integer COUNTER_WIDTH   = 64,
    parameter integer SEQUENCE_WIDTH  = 64
) (
    input  wire                         clk,
    input  wire                         rst,
    input  wire                         arm,
    input  wire                         s_valid,
    input  wire [DATA_WIDTH-1:0]        s_data,
    output wire                         s_ready,
    output logic                        m_valid,
    input  wire                         m_ready,
    output logic [DATA_WIDTH-1:0]       m_data,
    output logic                        m_last,
    output logic [SEQUENCE_WIDTH-1:0]   m_sequence,
    output logic [COUNTER_WIDTH-1:0]    input_bytes,
    output logic [COUNTER_WIDTH-1:0]    output_bytes,
    output logic [COUNTER_WIDTH-1:0]    dropped_bytes,
    output logic [COUNTER_WIDTH-1:0]    unarmed_bytes,
    output logic [COUNTER_WIDTH-1:0]    overflow_chunks,
    output logic [COUNTER_WIDTH-1:0]    aborted_chunks,
    output logic [63:0]                 buffered_bytes,
    output logic                        overflow_sticky,
    output logic                        counter_saturated,
    output logic                        sequence_exhausted
);

    localparam integer BYTES_PER_BEAT = DATA_WIDTH / 8;
    localparam integer BANK_WIDTH = (BANKS < 2) ? 1 : $clog2(BANKS);
    localparam integer CHUNK_COUNT_WIDTH = (CHUNK_BEATS < 2) ? 1 : $clog2(CHUNK_BEATS + 1);
    localparam integer RAM_DEPTH = BANKS * CHUNK_BEATS;
    localparam integer RAM_ADDR_WIDTH = (RAM_DEPTH < 2) ? 1 : $clog2(RAM_DEPTH);

    localparam logic [1:0] BANK_FREE     = 2'd0;
    localparam logic [1:0] BANK_FILLING  = 2'd1;
    localparam logic [1:0] BANK_READY    = 2'd2;
    localparam logic [1:0] BANK_READING  = 2'd3;
    localparam logic [COUNTER_WIDTH-1:0] COUNTER_MAX = {COUNTER_WIDTH{1'b1}};
    localparam logic [SEQUENCE_WIDTH-1:0] SEQUENCE_MAX = {SEQUENCE_WIDTH{1'b1}};

    // One write port and one registered read port. This array is deliberately not reset;
    // bank ownership is reset instead, so no uninitialized word can become output-valid.
    logic [DATA_WIDTH-1:0] staging_ram [0:RAM_DEPTH-1];
    logic [1:0] bank_state [0:BANKS-1];
    logic [SEQUENCE_WIDTH-1:0] bank_sequence [0:BANKS-1];

    logic [SEQUENCE_WIDTH-1:0] next_sequence;
    logic capture_active;
    logic capture_dropping;
    logic [BANK_WIDTH-1:0] capture_bank;
    logic [CHUNK_COUNT_WIDTH-1:0] capture_count;

    logic output_active;
    logic [BANK_WIDTH-1:0] output_bank;
    logic [CHUNK_COUNT_WIDTH-1:0] output_read_index;

    logic input_armed_valid;
    logic first_chunk_beat;
    logic free_bank_found;
    logic [BANK_WIDTH-1:0] free_bank;
    logic ready_bank_found;
    logic [BANK_WIDTH-1:0] ready_bank;
    logic [SEQUENCE_WIDTH-1:0] ready_sequence;
    logic output_fire;
    logic output_release;
    logic write_beat;
    logic write_first;
    logic [BANK_WIDTH-1:0] write_bank;
    logic write_last;
    logic drop_beat;
    logic overflow_event;
    logic abort_chunk;
    logic abort_partial;
    logic read_issue;
    logic read_first;
    logic [BANK_WIDTH-1:0] read_bank;
    logic [CHUNK_COUNT_WIDTH-1:0] read_index;
    logic [RAM_ADDR_WIDTH-1:0] write_address;
    logic [RAM_ADDR_WIDTH-1:0] read_address;
    logic [63:0] partial_bytes;
    logic [63:0] occupancy_added;
    logic [63:0] occupancy_removed;
    integer comb_i;
    integer seq_i;

    assign s_ready = !rst;

    function automatic [COUNTER_WIDTH-1:0] saturating_add(
        input logic [COUNTER_WIDTH-1:0] current_value,
        input logic [63:0] increment
    );
        begin
            if (increment > (COUNTER_MAX - current_value))
                saturating_add = COUNTER_MAX;
            else
                saturating_add = current_value + increment;
        end
    endfunction

    function automatic logic add_saturates(
        input logic [COUNTER_WIDTH-1:0] current_value,
        input logic [63:0] increment
    );
        begin
            add_saturates = increment > (COUNTER_MAX - current_value);
        end
    endfunction

    // Parameter errors are elaboration/configuration errors, not run-time protocol events.
    initial begin
        if (DATA_WIDTH <= 0 || (DATA_WIDTH % 8) != 0)
            $fatal(1, "DATA_WIDTH must be a positive multiple of 8");
        if (CHUNK_BEATS <= 0)
            $fatal(1, "CHUNK_BEATS must be positive");
        if (BANKS < 2)
            $fatal(1, "BANKS must be at least 2");
        if (COUNTER_WIDTH < 2)
            $fatal(1, "COUNTER_WIDTH must be at least 2");
        if (SEQUENCE_WIDTH < 2)
            $fatal(1, "SEQUENCE_WIDTH must be at least 2");
    end

    always_comb begin
        input_armed_valid = arm && s_valid && !rst;
        first_chunk_beat = input_armed_valid && !capture_active;
        output_fire = m_valid && m_ready && !rst;
        output_release = output_fire && m_last;

        // A bank released by this edge is eligible for a first capture beat on the same
        // edge. The old final output beat is already in the output register; the new bank
        // is not eligible for a RAM read until it has been sealed on a later edge.
        free_bank_found = 1'b0;
        free_bank = '0;
        for (comb_i = 0; comb_i < BANKS; comb_i = comb_i + 1) begin
            if (!free_bank_found &&
                ((bank_state[comb_i] == BANK_FREE) ||
                 (output_release && output_bank == comb_i))) begin
                free_bank_found = 1'b1;
                free_bank = comb_i[BANK_WIDTH-1:0];
            end
        end

        // Completed chunks are ordered by their non-wrapping source sequence. Only small
        // metadata participates in this selection; payload data is read through the
        // synchronous RAM port below, never through a combinational whole-bank mux.
        ready_bank_found = 1'b0;
        ready_bank = '0;
        ready_sequence = '1;
        for (comb_i = 0; comb_i < BANKS; comb_i = comb_i + 1) begin
            if (bank_state[comb_i] == BANK_READY &&
                (!ready_bank_found || bank_sequence[comb_i] < ready_sequence)) begin
                ready_bank_found = 1'b1;
                ready_bank = comb_i[BANK_WIDTH-1:0];
                ready_sequence = bank_sequence[comb_i];
            end
        end

        write_beat = 1'b0;
        write_first = 1'b0;
        write_bank = '0;
        write_last = 1'b0;
        drop_beat = 1'b0;
        overflow_event = 1'b0;
        write_address = '0;

        if (input_armed_valid) begin
            if (capture_active) begin
                if (capture_dropping) begin
                    drop_beat = 1'b1;
                end else begin
                    write_beat = 1'b1;
                    write_bank = capture_bank;
                    write_address = write_bank * CHUNK_BEATS + capture_count;
                    write_last = (capture_count == CHUNK_BEATS - 1);
                end
            end else if (!sequence_exhausted && free_bank_found) begin
                write_beat = 1'b1;
                write_first = 1'b1;
                write_bank = free_bank;
                write_address = write_bank * CHUNK_BEATS;
                write_last = (CHUNK_BEATS == 1);
            end else begin
                drop_beat = 1'b1;
                overflow_event = 1'b1;
            end
        end

        abort_chunk = !rst && !arm && capture_active;
        abort_partial = abort_chunk && !capture_dropping;
        partial_bytes = capture_count * BYTES_PER_BEAT;

        // Refill the one-beat output register whenever it will be empty after this edge.
        // On a final-beat handshake, select the next READY bank immediately so queued
        // chunk boundaries sustain one output beat per cycle.
        read_issue = 1'b0;
        read_first = 1'b0;
        read_bank = output_bank;
        read_index = output_read_index;
        if (!rst && (!m_valid || m_ready)) begin
            if (output_active) begin
                if (output_release) begin
                    if (ready_bank_found) begin
                        read_issue = 1'b1;
                        read_first = 1'b1;
                        read_bank = ready_bank;
                        read_index = '0;
                    end
                end else if (output_read_index < CHUNK_BEATS) begin
                    read_issue = 1'b1;
                    read_bank = output_bank;
                    read_index = output_read_index;
                end
            end else if (ready_bank_found) begin
                read_issue = 1'b1;
                read_first = 1'b1;
                read_bank = ready_bank;
                read_index = '0;
            end
        end
        read_address = read_bank * CHUNK_BEATS + read_index;

        occupancy_added = write_beat ? BYTES_PER_BEAT : 64'd0;
        occupancy_removed = output_fire ? BYTES_PER_BEAT : 64'd0;
        if (abort_partial)
            occupancy_removed = occupancy_removed + partial_bytes;
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            next_sequence       <= '0;
            capture_active      <= 1'b0;
            capture_dropping    <= 1'b0;
            capture_bank        <= '0;
            capture_count       <= '0;
            output_active       <= 1'b0;
            output_bank         <= '0;
            output_read_index   <= '0;
            m_valid             <= 1'b0;
            m_data              <= '0;
            m_last              <= 1'b0;
            m_sequence          <= '0;
            input_bytes         <= '0;
            output_bytes        <= '0;
            dropped_bytes       <= '0;
            unarmed_bytes       <= '0;
            overflow_chunks     <= '0;
            aborted_chunks      <= '0;
            buffered_bytes      <= '0;
            overflow_sticky     <= 1'b0;
            counter_saturated   <= 1'b0;
            sequence_exhausted  <= 1'b0;
            for (seq_i = 0; seq_i < BANKS; seq_i = seq_i + 1) begin
                bank_state[seq_i] <= BANK_FREE;
                bank_sequence[seq_i] <= '0;
            end
        end else begin
            // Do not clear staging_ram here. Only READY/READING bank metadata can
            // publish data, and reset returns every bank to FREE.
            if (write_beat)
                staging_ram[write_address] <= s_data;

            if (input_armed_valid) begin
                input_bytes <= saturating_add(input_bytes, BYTES_PER_BEAT);
                if (add_saturates(input_bytes, BYTES_PER_BEAT))
                    counter_saturated <= 1'b1;
            end else if (s_valid && !arm) begin
                unarmed_bytes <= saturating_add(unarmed_bytes, BYTES_PER_BEAT);
                if (add_saturates(unarmed_bytes, BYTES_PER_BEAT))
                    counter_saturated <= 1'b1;
            end

            if (output_fire) begin
                output_bytes <= saturating_add(output_bytes, BYTES_PER_BEAT);
                if (add_saturates(output_bytes, BYTES_PER_BEAT))
                    counter_saturated <= 1'b1;
            end

            if (drop_beat) begin
                dropped_bytes <= saturating_add(dropped_bytes, BYTES_PER_BEAT);
                if (add_saturates(dropped_bytes, BYTES_PER_BEAT))
                    counter_saturated <= 1'b1;
            end else if (abort_partial) begin
                dropped_bytes <= saturating_add(dropped_bytes, partial_bytes);
                if (add_saturates(dropped_bytes, partial_bytes))
                    counter_saturated <= 1'b1;
            end

            if (overflow_event) begin
                overflow_chunks <= saturating_add(overflow_chunks, 64'd1);
                overflow_sticky <= 1'b1;
                if (add_saturates(overflow_chunks, 64'd1))
                    counter_saturated <= 1'b1;
            end
            if (abort_chunk) begin
                aborted_chunks <= saturating_add(aborted_chunks, 64'd1);
                if (add_saturates(aborted_chunks, 64'd1))
                    counter_saturated <= 1'b1;
            end

            if (buffered_bytes + occupancy_added < occupancy_removed)
                buffered_bytes <= 64'd0; // Defensive only; correct ownership never underflows.
            else
                buffered_bytes <= buffered_bytes + occupancy_added - occupancy_removed;

            if (first_chunk_beat) begin
                if (!sequence_exhausted) begin
                    if (next_sequence == SEQUENCE_MAX)
                        sequence_exhausted <= 1'b1;
                    else
                        next_sequence <= next_sequence + 1'b1;
                end

                if (write_beat) begin
                    bank_sequence[write_bank] <= next_sequence;
                    if (write_last) begin
                        capture_active <= 1'b0;
                        capture_dropping <= 1'b0;
                        capture_count <= '0;
                    end else begin
                        capture_active <= 1'b1;
                        capture_dropping <= 1'b0;
                        capture_bank <= free_bank;
                        capture_count <= {{(CHUNK_COUNT_WIDTH-1){1'b0}}, 1'b1};
                    end
                end else begin
                    if (CHUNK_BEATS == 1) begin
                        capture_active <= 1'b0;
                        capture_dropping <= 1'b0;
                        capture_count <= '0;
                    end else begin
                        capture_active <= 1'b1;
                        capture_dropping <= 1'b1;
                        capture_count <= {{(CHUNK_COUNT_WIDTH-1){1'b0}}, 1'b1};
                    end
                end
            end else if (input_armed_valid && capture_active) begin
                if (capture_dropping) begin
                    if (capture_count == CHUNK_BEATS - 1) begin
                        capture_active <= 1'b0;
                        capture_dropping <= 1'b0;
                        capture_count <= '0;
                    end else begin
                        capture_count <= capture_count + 1'b1;
                    end
                end else if (write_last) begin
                    capture_active <= 1'b0;
                    capture_dropping <= 1'b0;
                    capture_count <= '0;
                end else begin
                    capture_count <= capture_count + 1'b1;
                end
            end else if (abort_chunk) begin
                capture_active <= 1'b0;
                capture_dropping <= 1'b0;
                capture_count <= '0;
            end

            // Bank ownership changes. A write to a bank released on this same edge has
            // priority, allowing immediate reuse without exposing stale RAM contents.
            for (seq_i = 0; seq_i < BANKS; seq_i = seq_i + 1) begin
                if (abort_partial && capture_bank == seq_i) begin
                    bank_state[seq_i] <= BANK_FREE;
                end else if (write_beat && write_bank == seq_i && !write_first) begin
                    bank_state[seq_i] <= write_last ? BANK_READY : BANK_FILLING;
                end else if (write_beat && write_bank == seq_i && write_first) begin
                    bank_state[seq_i] <= write_last ? BANK_READY : BANK_FILLING;
                end else if (read_issue && read_first && read_bank == seq_i) begin
                    bank_state[seq_i] <= BANK_READING;
                end else if (output_release && output_bank == seq_i) begin
                    bank_state[seq_i] <= BANK_FREE;
                end
            end

            if (read_issue) begin
                // This is the only payload read port and it is synchronous for block-RAM
                // inference. A bank becomes READING before any of its words are exposed.
                m_data <= staging_ram[read_address];
                m_last <= (read_index == CHUNK_BEATS - 1);
                m_sequence <= bank_sequence[read_bank];
                m_valid <= 1'b1;

                if (read_first) begin
                    output_active <= 1'b1;
                    output_bank <= read_bank;
                    output_read_index <= {{(CHUNK_COUNT_WIDTH-1){1'b0}}, 1'b1};
                end else begin
                    output_read_index <= output_read_index + 1'b1;
                end
            end else if (m_ready) begin
                m_valid <= 1'b0;
            end

            if (output_release && !read_issue) begin
                output_active <= 1'b0;
                output_read_index <= '0;
            end
        end
    end

endmodule
