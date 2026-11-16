"""Source fixtures: synthetic CBR transport streams, encoded once and paced on their PCR by TSDuck.

The primary tier is 1080p25 MPEG-2 at 17 Mb/s with MPEG-1 Layer II audio in a 20 Mb/s null-padded
multiplex: 1 s closed GOP, PAT/PMT every 100 ms, PCR every 20 ms. The H.264 tier has the same
structure for lanes that repackage H.264 (LOC).
"""
from dataclasses import dataclass

GROUP_IN = '239.79.0.1'
PORT_IN = 5000


@dataclass(frozen=True)
class Tier:
    name: str
    codec: str
    size: str
    video_k: int
    mux_bps: int
    audio: str = 'mp2'

    def ffmpeg_args(self, seconds):
        """FFmpeg arguments that write the tier for `seconds` to stdout as MPEG-TS."""
        v = (['-c:v', 'mpeg2video'] if self.codec == 'mpeg2v'
             else ['-c:v', 'libx264', '-preset', 'veryfast', '-x264-params', 'nal-hrd=cbr:force-cfr=1',
                   '-keyint_min', '25', '-sc_threshold', '0'])
        a = ['-c:a', 'mp2', '-b:a', '192k'] if self.audio == 'mp2' else ['-c:a', 'aac', '-b:a', '192k']
        return ['-hide_banner', '-nostdin', '-f', 'lavfi', '-i', f'testsrc2=size={self.size}:rate=25',
                '-f', 'lavfi', '-i', 'sine=f=1000:sample_rate=48000', '-t', str(int(seconds)), *v,
                '-b:v', f'{self.video_k}k', '-maxrate', f'{self.video_k}k', '-minrate', f'{self.video_k}k',
                '-bufsize', '6000k', '-g', '25', '-bf', '0', *a, '-muxrate', str(self.mux_bps),
                '-mpegts_service_id', '1', '-pat_period', '0.1', '-pcr_period', '20', '-f', 'mpegts']

    @property
    def pps(self):
        """1316-byte datagrams per second."""
        return self.mux_bps / (1316 * 8)

    def file(self, seconds):
        return f'{self.name}-{int(seconds)}s.ts'


TIERS = {t.name: t for t in (
    Tier('mpeg2-20m', 'mpeg2v', '1920x1080', 17000, 20_000_000),
    Tier('mpeg2-8m', 'mpeg2v', '1280x720', 6500, 8_000_000),
    Tier('mpeg2-40m', 'mpeg2v', '1920x1080', 36000, 40_000_000),
    Tier('h264-20m', 'h264', '1920x1080', 17000, 20_000_000, audio='aac'),
)}


def tsp_cmd(path, dest=f'{GROUP_IN}:{PORT_IN}'):
    """Pace a TS file or FIFO on its PCR to the source group in 7-packet datagrams."""
    return (f'tsp --buffer-size-mb 1 -I file {path} -P regulate --pcr-synchronous '
            f'-O ip --packet-burst 7 --ttl 1 {dest}')
