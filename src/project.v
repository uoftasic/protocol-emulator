/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Parameterized UART controller.
//
// Frames are transmitted LSB first, with an idle-high line.  When enabled,
// parity is even parity.  A transfer is accepted only while the transmitter
// is idle; i_tx_en is expected to be a one-clock pulse.
module uart_controller #(
    parameter integer CLK_PER_BIT   = 434,
    parameter integer DATA_WIDTH    = 8,
    parameter integer NUM_DATA_BITS = DATA_WIDTH,
    parameter integer NUM_STOP_BITS = 1,
    parameter integer PARITY_EN     = 0
) (
    input  wire                         clk,
    input  wire                         rst,
    input  wire                         ares,
    input  wire                         i_uart_rx,
    output wire                         o_uart_tx,
    input  wire [DATA_WIDTH-1:0]       i_tx_data,
    input  wire                         i_tx_en,
    output reg  [DATA_WIDTH-1:0]       o_rx_data,
    output reg                          o_rx_ready
);

  localparam integer BIT_COUNT_WIDTH = (CLK_PER_BIT <= 1) ? 1 : $clog2(CLK_PER_BIT);
  localparam integer FRAME_BITS = 1 + NUM_DATA_BITS + PARITY_EN + NUM_STOP_BITS;
  localparam integer FRAME_COUNT_WIDTH = (FRAME_BITS <= 2) ? 1 : $clog2(FRAME_BITS);
  localparam integer STOP_COUNT_WIDTH = (NUM_STOP_BITS <= 1) ? 1 : $clog2(NUM_STOP_BITS);
  localparam integer RX_BIT_COUNT_WIDTH = (NUM_DATA_BITS <= 1) ? 1 : $clog2(NUM_DATA_BITS);

  initial begin
    if (CLK_PER_BIT < 1 || NUM_DATA_BITS < 1 || NUM_STOP_BITS < 1 ||
        NUM_STOP_BITS > 2 || DATA_WIDTH < NUM_DATA_BITS) begin
      $error("Invalid UART parameters");
    end
  end

  // TX
  reg [BIT_COUNT_WIDTH-1:0] tx_clk_count;
  reg [FRAME_COUNT_WIDTH-1:0] tx_frame_position;
  reg [DATA_WIDTH-1:0] tx_data_reg;
  reg tx_parity;
  reg tx_active;
  reg tx_serial_reg;

  // Declared after the TX registers it reads: Icarus rejects use-before-declaration.
  function automatic uart_tx_symbol;
    input integer position;
    integer data_position;
    begin
      if (position == 0) begin
        uart_tx_symbol = 1'b0;
      end else if (position <= NUM_DATA_BITS) begin
        data_position = position - 1;
        uart_tx_symbol = tx_data_reg[data_position];
      end else if (PARITY_EN && position == NUM_DATA_BITS + 1) begin
        uart_tx_symbol = tx_parity;
      end else begin
        uart_tx_symbol = 1'b1;
      end
    end
  endfunction

  assign o_uart_tx = tx_serial_reg;

  always @(posedge clk or posedge ares) begin
    if (ares) begin
      tx_clk_count      <= 0;
      tx_frame_position <= 0;
      tx_data_reg       <= 0;
      tx_parity         <= 0;
      tx_active         <= 1'b0;
      tx_serial_reg     <= 1'b1;
    end else if (rst) begin
      tx_clk_count      <= 0;
      tx_frame_position <= 0;
      tx_data_reg       <= 0;
      tx_parity         <= 0;
      tx_active         <= 1'b0;
      tx_serial_reg     <= 1'b1;
    end else if (!tx_active) begin
      tx_clk_count <= 0;
      if (i_tx_en) begin
        tx_data_reg       <= i_tx_data;
        tx_parity         <= ^i_tx_data[NUM_DATA_BITS-1:0];
        tx_frame_position <= 0;
        tx_active         <= 1'b1;
        tx_serial_reg     <= 1'b0;
      end else begin
        tx_serial_reg <= 1'b1;
      end
    end else if (tx_clk_count == CLK_PER_BIT - 1) begin
      tx_clk_count <= 0;
      if (tx_frame_position == FRAME_BITS - 1) begin
        tx_active     <= 1'b0;
        tx_serial_reg <= 1'b1;
      end else begin
        tx_frame_position <= tx_frame_position + 1'b1;
        tx_serial_reg     <= uart_tx_symbol(tx_frame_position + 1);
      end
    end else begin
      tx_clk_count <= tx_clk_count + 1'b1;
    end
  end

  // RX
  localparam [2:0] RX_IDLE = 3'd0, RX_START = 3'd1, RX_DATA = 3'd2,
                   RX_PARITY = 3'd3, RX_STOP = 3'd4;
  reg [2:0] rx_state;
  reg [BIT_COUNT_WIDTH-1:0] rx_clk_count;
  reg [RX_BIT_COUNT_WIDTH-1:0] rx_bit_position;
  reg [STOP_COUNT_WIDTH-1:0] rx_stop_position;
  reg [DATA_WIDTH-1:0] rx_data_reg;
  reg rx_parity;
  reg rx_parity_error;

  always @(posedge clk or posedge ares) begin
    if (ares) begin
      rx_state         <= RX_IDLE;
      rx_clk_count     <= 0;
      rx_bit_position  <= 0;
      rx_stop_position <= 0;
      rx_data_reg      <= 0;
      rx_parity        <= 0;
      rx_parity_error  <= 1'b0;
      o_rx_data        <= 0;
      o_rx_ready       <= 1'b0;
    end else if (rst) begin
      rx_state         <= RX_IDLE;
      rx_clk_count     <= 0;
      rx_bit_position  <= 0;
      rx_stop_position <= 0;
      rx_data_reg      <= 0;
      rx_parity        <= 0;
      rx_parity_error  <= 1'b0;
      o_rx_data        <= 0;
      o_rx_ready       <= 1'b0;
    end else begin
      o_rx_ready <= 1'b0;
      case (rx_state)
        RX_IDLE: begin
          rx_clk_count <= 0;
          if (!i_uart_rx) begin
            rx_state <= RX_START;
            rx_parity_error <= 1'b0;
          end
        end

        RX_START: begin
          if (rx_clk_count == (CLK_PER_BIT / 2) - 1) begin
            rx_clk_count <= 0;
            rx_state <= i_uart_rx ? RX_IDLE : RX_DATA;
            rx_bit_position <= 0;
            rx_parity <= 0;
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_DATA: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count <= 0;
            rx_data_reg[rx_bit_position] <= i_uart_rx;
            rx_parity <= rx_parity ^ i_uart_rx;
            if (rx_bit_position == NUM_DATA_BITS - 1) begin
              rx_state <= PARITY_EN ? RX_PARITY : RX_STOP;
              rx_stop_position <= 0;
            end else begin
              rx_bit_position <= rx_bit_position + 1'b1;
            end
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_PARITY: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count <= 0;
            rx_parity_error <= (i_uart_rx != rx_parity);
            rx_stop_position <= 0;
            rx_state <= RX_STOP;
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        RX_STOP: begin
          if (rx_clk_count == CLK_PER_BIT - 1) begin
            rx_clk_count <= 0;
            if (rx_stop_position == NUM_STOP_BITS - 1) begin
              if (i_uart_rx && !rx_parity_error) begin
                o_rx_data <= rx_data_reg;
                o_rx_ready <= 1'b1;
              end
              rx_state <= RX_IDLE;
            end else begin
              rx_stop_position <= rx_stop_position + 1'b1;
            end
          end else begin
            rx_clk_count <= rx_clk_count + 1'b1;
          end
        end

        default: rx_state <= RX_IDLE;
      endcase
    end
  end
endmodule

// Tiny Tapeout pin adapter:
// ui_in      = transmit data
// uio_in[0]  = UART RX, uio_in[1] = transmit enable
// uo_out     = received data
// uio_out[0] = UART TX
module tt_um_uoftasic_protocol_emulator (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);
  wire [7:0] rx_data;

  uart_controller #(
      .CLK_PER_BIT(434),
      .DATA_WIDTH(8),
      .NUM_DATA_BITS(8),
      .NUM_STOP_BITS(1),
      .PARITY_EN(0)
  ) uart (
      .clk       (clk),
      .rst       (1'b0),
      .ares      (!rst_n),
      .i_uart_rx (uio_in[0]),
      .o_uart_tx (uio_out[0]),
      .i_tx_data (ui_in),
      .i_tx_en   (uio_in[1] && ena),
      .o_rx_data (rx_data),
      .o_rx_ready()
  );

  assign uo_out  = rx_data;
  assign uio_out[7:1] = 7'b0;
  assign uio_oe  = 8'b0000_0001;
endmodule

`default_nettype wire
