// SPDX-License-Identifier: BSD-2-Clause
// Portable single-job AXI writer for a fenced PL-DDR capture aperture.
module capture_ddr_writer #(
    parameter integer DATA_WIDTH = 256,
    parameter integer ADDR_WIDTH = 32,
    parameter integer CHUNK_BEATS = 128,
    parameter integer SLOTS = 8,
    parameter logic [63:0] SLOT_STRIDE_BYTES = 64'd4096,
    parameter logic [31:0] REGION_BASE = 32'd0,
    parameter logic [63:0] APERTURE_BYTES = 64'd65536,
    parameter integer MAX_BURST_BEATS = 16,
    parameter integer LEASE_WIDTH = 64,
    parameter integer EPOCH_WIDTH = 32,
    parameter integer SEQUENCE_WIDTH = 64
) (
    input  logic clk,
    input  logic rst,

    input  logic job_valid,
    output logic job_ready,
    input  logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] job_slot,
    input  logic [LEASE_WIDTH-1:0] job_lease,
    input  logic [EPOCH_WIDTH-1:0] job_epoch,
    input  logic [SEQUENCE_WIDTH-1:0] job_sequence,

    input  logic s_valid,
    output logic s_ready,
    input  logic [DATA_WIDTH-1:0] s_data,
    input  logic s_last,
    input  logic [SEQUENCE_WIDTH-1:0] s_sequence,

    output logic axi_aw_valid,
    input  logic axi_aw_ready,
    output logic [31:0] axi_aw_addr,
    output logic [7:0] axi_aw_len,
    output logic [2:0] axi_aw_size,
    output logic [1:0] axi_aw_burst,
    output logic [0:0] axi_aw_id,

    output logic axi_w_valid,
    input  logic axi_w_ready,
    output logic [DATA_WIDTH-1:0] axi_w_data,
    output logic [DATA_WIDTH/8-1:0] axi_w_strb,
    output logic axi_w_last,

    input  logic axi_b_valid,
    output logic axi_b_ready,
    input  logic [1:0] axi_b_resp,
    input  logic [0:0] axi_b_id,

    input  logic visibility_ready,
    output logic commit_valid,
    input  logic commit_ready,
    output logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] commit_slot,
    output logic [LEASE_WIDTH-1:0] commit_lease,
    output logic [EPOCH_WIDTH-1:0] commit_epoch,
    output logic [SEQUENCE_WIDTH-1:0] commit_sequence,

    input  logic abort_req,
    output logic faulted,
    output logic [3:0] error_code,
    output logic axi_quiescent,
    output logic stream_drained,
    input  logic recover_valid,
    output logic recover_ready,
    input  logic fence_done
);

    localparam integer BYTES_PER_BEAT =
        ((DATA_WIDTH == 64) || (DATA_WIDTH == 128) || (DATA_WIDTH == 256))
            ? (DATA_WIDTH / 8) : 1;
    localparam integer SLOT_WIDTH = (SLOTS <= 1) ? 1 : $clog2(SLOTS);
    localparam integer BUFFER_DEPTH =
        ((MAX_BURST_BEATS >= 1) && (MAX_BURST_BEATS <= 16))
            ? MAX_BURST_BEATS : 1;

    localparam logic [127:0] CHUNK_BEATS_WIDE = (CHUNK_BEATS > 0) ? CHUNK_BEATS : 0;
    localparam logic [127:0] SLOTS_MINUS_ONE_WIDE = (SLOTS > 0) ? (SLOTS - 1) : 0;
    localparam logic [127:0] SLOT_STRIDE_WIDE = SLOT_STRIDE_BYTES;
    localparam logic [127:0] APERTURE_WIDE = APERTURE_BYTES;
    localparam logic [127:0] REGION_BASE_WIDE = REGION_BASE;
    localparam logic [127:0] BYTES_PER_BEAT_WIDE = BYTES_PER_BEAT;
    localparam logic [127:0] CHUNK_BYTES_WIDE = CHUNK_BEATS_WIDE * BYTES_PER_BEAT_WIDE;
    localparam logic [127:0] LAST_SLOT_LOCAL_END_WIDE =
        SLOTS_MINUS_ONE_WIDE * SLOT_STRIDE_WIDE + CHUNK_BYTES_WIDE;
    localparam logic [127:0] LAST_SLOT_ABSOLUTE_BYTE_WIDE =
        REGION_BASE_WIDE + LAST_SLOT_LOCAL_END_WIDE - 1'b1;
    localparam logic [127:0] ADDR_MAX_WIDE = 128'h000000000000000000000000ffffffff;
    localparam integer AXI_SIZE = $clog2(BYTES_PER_BEAT);

    localparam logic [2:0] ST_IDLE    = 3'd0;
    localparam logic [2:0] ST_COLLECT = 3'd1;
    localparam logic [2:0] ST_SEND    = 3'd2;
    localparam logic [2:0] ST_WAIT_B  = 3'd3;
    localparam logic [2:0] ST_COMMIT  = 3'd4;
    localparam logic [2:0] ST_DRAIN   = 3'd5;

    localparam logic [3:0] ERR_NONE     = 4'd0;
    localparam logic [3:0] ERR_BAD_JOB  = 4'd1;
    localparam logic [3:0] ERR_FRAMING  = 4'd2;
    localparam logic [3:0] ERR_AXI      = 4'd3;
    localparam logic [3:0] ERR_ABORT    = 4'd4;

    localparam logic PARAMETERS_VALID =
        ((DATA_WIDTH == 64) || (DATA_WIDTH == 128) || (DATA_WIDTH == 256)) &&
        (ADDR_WIDTH == 32) &&
        (CHUNK_BEATS > 0) &&
        (SLOTS > 0) &&
        (SLOT_STRIDE_BYTES > 0) &&
        (SLOT_STRIDE_BYTES >= CHUNK_BYTES_WIDE) &&
        ((SLOT_STRIDE_BYTES % BYTES_PER_BEAT) == 0) &&
        ((REGION_BASE % BYTES_PER_BEAT) == 0) &&
        (APERTURE_BYTES > 0) &&
        (MAX_BURST_BEATS >= 1) && (MAX_BURST_BEATS <= 16) &&
        (LEASE_WIDTH >= 2) && (EPOCH_WIDTH >= 2) && (SEQUENCE_WIDTH >= 2) &&
        (REGION_BASE_WIDE + LAST_SLOT_LOCAL_END_WIDE <= APERTURE_WIDE) &&
        (LAST_SLOT_ABSOLUTE_BYTE_WIDE <= ADDR_MAX_WIDE);

    generate
        if (!PARAMETERS_VALID) begin : g_invalid_parameters
            initial $fatal(1, "capture_ddr_writer parameter constraints violated");
        end
    endgenerate

    logic [2:0] state;
    logic [DATA_WIDTH-1:0] burst_buffer [0:BUFFER_DEPTH-1];
    logic [((SLOTS <= 1) ? 1 : $clog2(SLOTS))-1:0] job_slot_reg;
    logic [LEASE_WIDTH-1:0] job_lease_reg;
    logic [EPOCH_WIDTH-1:0] job_epoch_reg;
    logic [SEQUENCE_WIDTH-1:0] job_sequence_reg;
    logic [31:0] burst_address;
    logic [31:0] source_beats_seen;
    logic [31:0] completed_beats;
    logic [4:0] burst_beats;
    logic [4:0] buffered_beats;
    logic [4:0] w_index;
    logic aw_done;
    logic w_done;
    logic source_chunk_active;
    logic publication_started;

    logic job_slot_invalid;
    logic bad_job;
    logic framing_error;
    logic bad_b_response;
    logic fault_event;
    logic [3:0] fault_event_code;
    logic s_fire;
    logic aw_fire;
    logic w_fire;
    logic w_last_fire;
    logic b_fire;
    logic commit_fire;
    logic [63:0] job_slot_offset;
    logic [63:0] job_address_wide;
    logic [31:0] completed_after_burst;
    logic [31:0] remaining_after_burst;
    logic [31:0] next_burst_address_wide;
    logic [4:0] next_burst_beats;

    function automatic [4:0] choose_burst_beats(
        input logic [31:0] start_address,
        input logic [31:0] beats_remaining
    );
        integer beats_to_boundary;
        integer result_beats;
        integer bytes_to_boundary;
        begin
            bytes_to_boundary = 4096 - start_address[11:0];
            beats_to_boundary = bytes_to_boundary / BYTES_PER_BEAT;
            result_beats = MAX_BURST_BEATS;
            if (result_beats > beats_remaining)
                result_beats = beats_remaining;
            if (result_beats > beats_to_boundary)
                result_beats = beats_to_boundary;
            choose_burst_beats = result_beats[4:0];
        end
    endfunction

    always_comb begin
        job_slot_invalid = (job_slot >= SLOTS);
        bad_job = job_valid && (job_slot_invalid || (job_lease == '0));

        job_slot_offset =
            {{(64-SLOT_WIDTH){1'b0}}, job_slot} * SLOT_STRIDE_BYTES;
        job_address_wide = {32'd0, REGION_BASE} + job_slot_offset;

        job_ready = !rst && !faulted && !abort_req && (state == ST_IDLE) && !bad_job;
        s_ready = 1'b0;
        if (!rst) begin
            if (faulted) begin
                s_ready = source_chunk_active;
            end else if (!abort_req && (state == ST_COLLECT)) begin
                s_ready = (buffered_beats < burst_beats);
            end
        end
        s_fire = s_valid && s_ready;

        axi_aw_valid = !rst && (state == ST_SEND) && !aw_done;
        axi_aw_addr = burst_address;
        axi_aw_len = {3'b000, (burst_beats - 5'd1)};
        axi_aw_size = AXI_SIZE[2:0];
        axi_aw_burst = 2'b01;
        axi_aw_id = 1'b0;

        axi_w_valid = !rst && (state == ST_SEND) && !w_done;
        axi_w_data = '0;
        if (axi_w_valid)
            axi_w_data = burst_buffer[w_index];
        axi_w_strb = '1;
        axi_w_last = axi_w_valid && (w_index == (burst_beats - 5'd1));

        axi_b_ready = !rst && (state == ST_WAIT_B);

        commit_valid = !rst && !faulted && !abort_req && (state == ST_COMMIT) &&
                       (publication_started || visibility_ready);
        commit_slot = job_slot_reg;
        commit_lease = job_lease_reg;
        commit_epoch = job_epoch_reg;
        commit_sequence = job_sequence_reg;

        axi_quiescent = (state != ST_SEND) && (state != ST_WAIT_B);
        stream_drained = !source_chunk_active;
        recover_ready = !rst && faulted && axi_quiescent && stream_drained &&
                        fence_done && !abort_req;

        framing_error = s_fire && !faulted && (state == ST_COLLECT) &&
                        ((s_sequence != job_sequence_reg) ||
                         (s_last != (source_beats_seen == (CHUNK_BEATS - 1))));
        bad_b_response = axi_b_valid && axi_b_ready &&
                         ((axi_b_resp != 2'b00) || (axi_b_id != 1'b0));

        fault_event = 1'b0;
        fault_event_code = ERR_NONE;
        if (!rst && !faulted) begin
            if (abort_req) begin
                fault_event = 1'b1;
                fault_event_code = ERR_ABORT;
            end else if ((state == ST_IDLE) && bad_job) begin
                fault_event = 1'b1;
                fault_event_code = ERR_BAD_JOB;
            end else if (framing_error) begin
                fault_event = 1'b1;
                fault_event_code = ERR_FRAMING;
            end else if (bad_b_response) begin
                fault_event = 1'b1;
                fault_event_code = ERR_AXI;
            end
        end

        aw_fire = axi_aw_valid && axi_aw_ready;
        w_fire = axi_w_valid && axi_w_ready;
        w_last_fire = w_fire && axi_w_last;
        b_fire = axi_b_valid && axi_b_ready;
        commit_fire = commit_valid && commit_ready;

        completed_after_burst = completed_beats + burst_beats;
        remaining_after_burst = 0;
        if (completed_after_burst < CHUNK_BEATS)
            remaining_after_burst = CHUNK_BEATS - completed_after_burst;
        next_burst_address_wide = burst_address + (burst_beats * BYTES_PER_BEAT);
        next_burst_beats = choose_burst_beats(next_burst_address_wide,
                                              remaining_after_burst);
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= ST_IDLE;
            faulted <= 1'b0;
            error_code <= ERR_NONE;
            job_slot_reg <= '0;
            job_lease_reg <= '0;
            job_epoch_reg <= '0;
            job_sequence_reg <= '0;
            burst_address <= '0;
            source_beats_seen <= '0;
            completed_beats <= '0;
            burst_beats <= '0;
            buffered_beats <= '0;
            w_index <= '0;
            aw_done <= 1'b0;
            w_done <= 1'b0;
            source_chunk_active <= 1'b0;
            publication_started <= 1'b0;
        end else begin
            if (!faulted && fault_event) begin
                faulted <= 1'b1;
                error_code <= fault_event_code;
            end

            if (s_fire && s_last)
                source_chunk_active <= 1'b0;

            case (state)
                ST_IDLE: begin
                    if (fault_event) begin
                        state <= ST_DRAIN;
                    end else if (job_valid && job_ready) begin
                        job_slot_reg <= job_slot;
                        job_lease_reg <= job_lease;
                        job_epoch_reg <= job_epoch;
                        job_sequence_reg <= job_sequence;
                        burst_address <= job_address_wide[31:0];
                        source_beats_seen <= '0;
                        completed_beats <= '0;
                        burst_beats <= choose_burst_beats(
                            job_address_wide[31:0], CHUNK_BEATS
                        );
                        buffered_beats <= '0;
                        w_index <= '0;
                        aw_done <= 1'b0;
                        w_done <= 1'b0;
                        source_chunk_active <= 1'b1;
                        publication_started <= 1'b0;
                        state <= ST_COLLECT;
                    end
                end

                ST_COLLECT: begin
                    if (fault_event) begin
                        buffered_beats <= '0;
                        state <= ST_DRAIN;
                        if (framing_error)
                            source_chunk_active <= !s_last;
                    end else if (s_fire) begin
                        burst_buffer[buffered_beats] <= s_data;
                        source_beats_seen <= source_beats_seen + 1'b1;
                        if (buffered_beats == (burst_beats - 5'd1)) begin
                            buffered_beats <= burst_beats;
                            w_index <= '0;
                            aw_done <= 1'b0;
                            w_done <= 1'b0;
                            state <= ST_SEND;
                        end else begin
                            buffered_beats <= buffered_beats + 1'b1;
                        end
                    end
                end

                ST_SEND: begin
                    if (aw_fire)
                        aw_done <= 1'b1;
                    if (w_fire) begin
                        if (axi_w_last)
                            w_done <= 1'b1;
                        else
                            w_index <= w_index + 1'b1;
                    end
                    if ((aw_done || aw_fire) && (w_done || w_last_fire))
                        state <= ST_WAIT_B;
                end

                ST_WAIT_B: begin
                    if (b_fire) begin
                        if (faulted || abort_req || bad_b_response) begin
                            buffered_beats <= '0;
                            state <= ST_DRAIN;
                        end else if (completed_after_burst == CHUNK_BEATS) begin
                            completed_beats <= completed_after_burst;
                            publication_started <= 1'b0;
                            state <= ST_COMMIT;
                        end else begin
                            completed_beats <= completed_after_burst;
                            burst_address <= next_burst_address_wide;
                            burst_beats <= next_burst_beats;
                            buffered_beats <= '0;
                            w_index <= '0;
                            aw_done <= 1'b0;
                            w_done <= 1'b0;
                            state <= ST_COLLECT;
                        end
                    end
                end

                ST_COMMIT: begin
                    if (fault_event) begin
                        publication_started <= 1'b0;
                        state <= ST_DRAIN;
                    end else begin
                        if (visibility_ready)
                            publication_started <= 1'b1;
                        if (commit_fire) begin
                            state <= ST_IDLE;
                            source_beats_seen <= '0;
                            completed_beats <= '0;
                            buffered_beats <= '0;
                            burst_beats <= '0;
                            publication_started <= 1'b0;
                            source_chunk_active <= 1'b0;
                        end
                    end
                end

                ST_DRAIN: begin
                    buffered_beats <= '0;
                    if (faulted && recover_valid && recover_ready) begin
                        faulted <= 1'b0;
                        error_code <= ERR_NONE;
                        state <= ST_IDLE;
                        job_slot_reg <= '0;
                        job_lease_reg <= '0;
                        job_epoch_reg <= '0;
                        job_sequence_reg <= '0;
                        source_beats_seen <= '0;
                        completed_beats <= '0;
                        burst_beats <= '0;
                        w_index <= '0;
                        aw_done <= 1'b0;
                        w_done <= 1'b0;
                        source_chunk_active <= 1'b0;
                        publication_started <= 1'b0;
                    end
                end

                default: begin
                    state <= ST_DRAIN;
                    if (!faulted) begin
                        faulted <= 1'b1;
                        error_code <= ERR_BAD_JOB;
                    end
                end
            endcase
        end
    end

endmodule
