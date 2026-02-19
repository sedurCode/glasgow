import logging
import asyncio
import wave
import struct

from amaranth import *
from amaranth.lib import wiring, stream, io
from amaranth.lib.wiring import In, Out
from amaranth.lib.cdc import FFSynchronizer

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletV2


class I2SInputComponent(wiring.Component):
    o_stream: Out(stream.Signature(8))

    def __init__(self, ports, bit_depth):
        self._ports = ports
        self.bit_depth = bit_depth
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.submodules.bclk_buffer = bclk_buffer = io.Buffer("i", self._ports.bclk)
        m.submodules.ws_buffer   = ws_buffer   = io.Buffer("i", self._ports.ws)
        m.submodules.sd_buffer   = sd_buffer   = io.Buffer("i", self._ports.sd)

        bclk = Signal()
        ws   = Signal()
        sd   = Signal()

        m.submodules += [
            FFSynchronizer(bclk_buffer.i, bclk),
            FFSynchronizer(ws_buffer.i,   ws),
            FFSynchronizer(sd_buffer.i,   sd),
        ]

        bclk_r = Signal()
        ws_r   = Signal()
        m.d.sync += [
            bclk_r.eq(bclk),
            ws_r.eq(ws),
        ]

        bclk_rising = bclk & ~bclk_r
        ws_edge     = ws ^ ws_r

        bit_counter = Signal(range(self.bit_depth + 1))
        shift_reg   = Signal(self.bit_depth)

        byte_index   = Signal(range(self.bit_depth // 8))
        output_reg   = Signal(self.bit_depth)

        with m.FSM():
            with m.State("WAIT-WS"):
                with m.If(ws_edge):
                    m.next = "WAIT-BCLK"

            with m.State("WAIT-BCLK"):
                with m.If(bclk_rising):
                    m.d.sync += [
                        shift_reg.eq(Cat(sd, shift_reg[:-1])),
                        bit_counter.eq(self.bit_depth - 1),
                    ]
                    m.next = "SAMPLE"

            with m.State("SAMPLE"):
                with m.If(bclk_rising):
                    m.d.sync += [
                        shift_reg.eq(Cat(sd, shift_reg[:-1])),
                        bit_counter.eq(bit_counter - 1)
                    ]
                    with m.If(bit_counter == 1):
                        m.d.sync += [
                            output_reg.eq(Cat(sd, shift_reg[:-1])),
                            byte_index.eq(self.bit_depth // 8 - 1)
                        ]
                        m.next = "OUTPUT"

            with m.State("OUTPUT"):
                m.d.comb += [
                    self.o_stream.payload.eq(output_reg.word_select(byte_index, 8)),
                    self.o_stream.valid.eq(1),
                ]
                with m.If(self.o_stream.ready):
                    with m.If(byte_index == 0):
                        m.next = "WAIT-WS"
                    with m.Else():
                        m.d.sync += byte_index.eq(byte_index - 1)

        return m


class I2SInputInterface:
    def __init__(self, logger, assembly, bclk, ws, sd, bit_depth):
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

        ports = assembly.add_port_group(bclk=bclk, ws=ws, sd=sd)
        component = assembly.add_submodule(I2SInputComponent(ports, bit_depth))
        self._pipe = assembly.add_in_pipe(component.o_stream)

    async def read(self, n):
        return await self._pipe.recv(n)


class I2SInputApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "record I2S audio"
    description = """
    Record I2S audio signals.
    """

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "bclk", default=True)
        access.add_pins_argument(parser, "ws", default=True)
        access.add_pins_argument(parser, "sd", default=True)
        parser.add_argument(
            "-b", "--bit-depth", metavar="BITS", type=int, default=16, choices=(16, 24, 32),
            help="set bit depth to BITS (default: %(default)d)")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.i2s_iface = I2SInputInterface(self.logger, self.assembly,
                                               bclk=args.bclk, ws=args.ws, sd=args.sd,
                                               bit_depth=args.bit_depth)

    @classmethod
    def add_run_arguments(cls, parser):
        parser.add_argument(
            "-r", "--sample-rate", metavar="RATE", type=int, default=44100,
            help="set sample rate to RATE Hz (default: %(default)d)")
        parser.add_argument(
            "-d", "--duration", metavar="DURATION", type=float,
            help="record for DURATION seconds (default: infinite)")
        parser.add_argument(
            "-o", "--output", metavar="FILE", required=True,
            help="write recorded audio to FILE (as .wav)")

    async def run(self, args):
        bit_depth = args.bit_depth
        sample_rate = args.sample_rate
        channels = 2 # I2S is always stereo (or at least 2 slots)

        # Open wave file
        wf = wave.open(args.output, "wb")
        wf.setnchannels(channels)
        wf.setsampwidth(bit_depth // 8)
        wf.setframerate(sample_rate)

        self.logger.info("recording to %s...", args.output)

        bytes_per_sample = bit_depth // 8
        bytes_per_frame = bytes_per_sample * channels

        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                if args.duration and (asyncio.get_event_loop().time() - start_time) >= args.duration:
                    break

                # Read one frame (Left + Right)
                data = await self.i2s_iface.read(bytes_per_frame)

                # PCM in WAV is usually little-endian.
                # Our gateware sends big-endian (MSB byte first).
                if bit_depth == 16:
                    data = bytes([data[1], data[0], data[3], data[2]])
                elif bit_depth == 24:
                    data = bytes([data[2], data[1], data[0], data[5], data[4], data[3]])
                elif bit_depth == 32:
                    data = bytes([data[3], data[2], data[1], data[0], data[7], data[6], data[5], data[4]])

                wf.writeframes(data)

        except asyncio.CancelledError:
            pass
        finally:
            wf.close()
            self.logger.info("finished recording")

    @classmethod
    def tests(cls):
        from . import test
        return test.I2SInputAppletTestCase
