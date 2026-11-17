/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// The UART controller lives in L1-PEm-system/L2-uart-controller/uart-controller.sv.

// Tiny Tapeout pin adapter (pinout from info.yaml):
//   ui_in[2]  = UART RX        uo_out[1] = UART TX
//   ui_in[0]  = MOSI, ui_in[1] = SCLK, ui_in[3] = CS_n   (SPI, not yet connected)
//   uo_out[0] = MISO, uo_out[2] = IRQ                     (not yet connected, held 0)
//
// Until the SPI host interface exists, the UART runs in echo mode: every byte
// received on RX is transmitted back on TX. A one-byte holding register keeps
// a received byte until the transmitter is free, so back-to-back frames at
// the same baud rate are all echoed. (If RX outpaces TX for longer than one
// byte, the held byte is overwritten.)
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
  wire       rx_ready;
  wire       uart_tx;
  wire       tx_busy;
  reg  [7:0] hold_data;
  reg        hold_valid;
  wire       tx_send = hold_valid && !tx_busy;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      hold_data  <= 8'b0;
      hold_valid <= 1'b0;
    end else begin
      if (tx_send)
        hold_valid <= 1'b0;
      if (rx_ready) begin  // a new byte wins over clearing the old one
        hold_data  <= rx_data;
        hold_valid <= 1'b1;
      end
    end
  end

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
      .i_uart_rx (ui_in[2]),
      .o_uart_tx (uart_tx),
      .i_tx_data (hold_data),
      .i_tx_en   (tx_send),
      .o_tx_busy (tx_busy),
      .o_rx_data (rx_data),
      .o_rx_ready(rx_ready)
  );

  assign uo_out  = {5'b0, 1'b0 /* IRQ */, uart_tx, 1'b0 /* MISO */};
  assign uio_out = 8'b0;
  assign uio_oe  = 8'b0;  // all uio pins are inputs (unused)

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in[7:3], ui_in[1:0], uio_in, 1'b0};
endmodule

`default_nettype wire
