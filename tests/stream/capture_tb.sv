// SPDX-License-Identifier: BSD-2-Clause
`timescale 1ns/1ps
module capture_tb #(
    parameter integer DATA_WIDTH = 8,
    parameter integer CHUNK_BEATS = 3,
    parameter integer BANKS = 2,
    parameter integer COUNTER_WIDTH = 16,
    parameter integer SEQUENCE_WIDTH = 64
) (
    input  wire                        clk,
    input  wire                        rst,
    input  wire                        arm,
    input  wire                        s_valid,
    input  wire [DATA_WIDTH-1:0]       s_data,
    output wire                        s_ready,
    output wire                        m_valid,
    input  wire                        m_ready,
    output wire [DATA_WIDTH-1:0]       m_data,
    output wire                        m_last,
    output wire [SEQUENCE_WIDTH-1:0]   m_sequence,
    output wire [COUNTER_WIDTH-1:0]    input_bytes,
    output wire [COUNTER_WIDTH-1:0]    output_bytes,
    output wire [COUNTER_WIDTH-1:0]    dropped_bytes,
    output wire [COUNTER_WIDTH-1:0]    unarmed_bytes,
    output wire [COUNTER_WIDTH-1:0]    overflow_chunks,
    output wire [COUNTER_WIDTH-1:0]    aborted_chunks,
    output wire [63:0]                 buffered_bytes,
    output wire                        overflow_sticky,
    output wire                        counter_saturated,
    output wire                        sequence_exhausted
);
    capture_stager #(
        .DATA_WIDTH(DATA_WIDTH),
        .CHUNK_BEATS(CHUNK_BEATS),
        .BANKS(BANKS),
        .COUNTER_WIDTH(COUNTER_WIDTH),
        .SEQUENCE_WIDTH(SEQUENCE_WIDTH)
    ) dut (.*);
endmodule
