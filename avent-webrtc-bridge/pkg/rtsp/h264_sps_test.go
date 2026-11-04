package rtsp

import (
	"encoding/base64"
	"testing"
)

// bitWriter builds SPS bodies field by field for the tests.
type bitWriter struct {
	b []byte
	n int
}

func (w *bitWriter) bit(v uint) {
	if w.n%8 == 0 {
		w.b = append(w.b, 0)
	}
	if v != 0 {
		w.b[len(w.b)-1] |= 1 << (7 - w.n%8)
	}
	w.n++
}

func (w *bitWriter) bits(v uint, n int) {
	for i := n - 1; i >= 0; i-- {
		w.bit(v >> i & 1)
	}
}

func (w *bitWriter) ue(v uint) {
	v++
	n := 0
	for x := v; x > 1; x >>= 1 {
		n++
	}
	w.bits(0, n)
	w.bits(v, n+1)
}

func (w *bitWriter) se(v int) {
	if v > 0 {
		w.ue(uint(2*v - 1))
	} else {
		w.ue(uint(-2 * v))
	}
}

// escape inserts emulation prevention bytes, as an encoder does.
func escape(rbsp []byte) []byte {
	var out []byte
	zeros := 0
	for _, c := range rbsp {
		if zeros >= 2 && c <= 3 {
			out = append(out, 3)
			zeros = 0
		}
		if c == 0 {
			zeros++
		} else {
			zeros = 0
		}
		out = append(out, c)
	}
	return out
}

type spsFields struct {
	profile        uint
	chroma         uint
	scaling        bool
	pocType        uint
	widthMBs       uint // minus 1
	heightMapUnits uint // minus 1
	frameMBsOnly   uint
	crop           [4]uint
}

func buildSPS(f spsFields) []byte {
	w := &bitWriter{}
	w.bits(f.profile, 8)
	w.bits(0, 8)  // constraint flags
	w.bits(31, 8) // level 3.1
	w.ue(0)       // sps id
	if highProfiles[f.profile] {
		w.ue(f.chroma)
		if f.chroma == 3 {
			w.bit(0)
		}
		w.ue(0)
		w.ue(0)
		w.bit(0)
		if f.scaling {
			w.bit(1)
			lists := 8
			if f.chroma == 3 {
				lists = 12
			}
			for i := 0; i < lists; i++ {
				if i == 0 || i == 6 {
					w.bit(1)
					size := 16
					if i == 6 {
						size = 64
					}
					for j := 0; j < size; j++ {
						w.se(1) // a rising list, never zero
					}
				} else {
					w.bit(0)
				}
			}
		} else {
			w.bit(0)
		}
	}
	w.ue(4) // log2_max_frame_num_minus4
	w.ue(f.pocType)
	switch f.pocType {
	case 0:
		w.ue(4)
	case 1:
		w.bit(0)
		w.se(-2)
		w.se(3)
		w.ue(2)
		w.se(1)
		w.se(-1)
	}
	w.ue(1) // max_num_ref_frames
	w.bit(0)
	w.ue(f.widthMBs)
	w.ue(f.heightMapUnits)
	w.bit(f.frameMBsOnly)
	if f.frameMBsOnly == 0 {
		w.bit(0)
	}
	w.bit(1) // direct_8x8_inference_flag
	if f.crop != [4]uint{} {
		w.bit(1)
		for _, c := range f.crop {
			w.ue(c)
		}
	} else {
		w.bit(0)
	}
	w.bit(0) // no VUI
	w.bit(1) // rbsp stop bit
	return append([]byte{0x67}, escape(w.b)...)
}

func TestSPSResolution(t *testing.T) {
	cases := []struct {
		name          string
		f             spsFields
		width, height int
	}{
		{"baseline 720p", spsFields{profile: 66, widthMBs: 79, heightMapUnits: 44, frameMBsOnly: 1}, 1280, 720},
		{"main 1080p cropped", spsFields{profile: 77, widthMBs: 119, heightMapUnits: 67, frameMBsOnly: 1, crop: [4]uint{0, 0, 0, 4}}, 1920, 1080},
		{"high 1080p with scaling lists", spsFields{profile: 100, chroma: 1, scaling: true, widthMBs: 119, heightMapUnits: 67, frameMBsOnly: 1, crop: [4]uint{0, 0, 0, 4}}, 1920, 1080},
		{"high 4:4:4 cropped", spsFields{profile: 244, chroma: 3, scaling: true, widthMBs: 39, heightMapUnits: 29, frameMBsOnly: 1, crop: [4]uint{1, 1, 2, 2}}, 638, 476},
		{"interlaced 1080i", spsFields{profile: 77, widthMBs: 119, heightMapUnits: 33, frameMBsOnly: 0, crop: [4]uint{0, 0, 0, 2}}, 1920, 1080},
		{"poc type 1", spsFields{profile: 66, pocType: 1, widthMBs: 39, heightMapUnits: 29, frameMBsOnly: 1}, 640, 480},
		{"poc type 2", spsFields{profile: 66, pocType: 2, widthMBs: 21, heightMapUnits: 17, frameMBsOnly: 1}, 352, 288},
	}
	for _, c := range cases {
		nal := buildSPS(c.f)
		w, h, ok := spsResolution(nal)
		if !ok || w != c.width || h != c.height {
			t.Errorf("%s: got %dx%d ok=%v, want %dx%d (sps % x)", c.name, w, h, ok, c.width, c.height, nal)
		}
	}
}

func TestSPSResolution_RealEncoderSPS(t *testing.T) {
	// A High@3.1 1280x720 SPS as x264 writes it, with emulation prevention
	// bytes in it.
	nal, _ := base64.StdEncoding.DecodeString("Z2QAH6zZQFAFuwEQAAADABAAAAMDwPGDGWA=")
	if w, h, ok := spsResolution(nal); !ok || w != 1280 || h != 720 {
		t.Fatalf("got %dx%d ok=%v, want 1280x720", w, h, ok)
	}
}

func TestSPSResolutionInPayload(t *testing.T) {
	sps := buildSPS(spsFields{profile: 66, widthMBs: 79, heightMapUnits: 44, frameMBsOnly: 1})
	pps := []byte{0x68, 0xce, 0x3c, 0x80}

	if w, h, ok := spsResolutionInPayload(sps); !ok || w != 1280 || h != 720 {
		t.Fatalf("whole SPS: %dx%d ok=%v", w, h, ok)
	}

	stap := []byte{24}
	for _, nal := range [][]byte{pps, sps} {
		stap = append(stap, byte(len(nal)>>8), byte(len(nal)))
		stap = append(stap, nal...)
	}
	if w, h, ok := spsResolutionInPayload(stap); !ok || w != 1280 || h != 720 {
		t.Fatalf("STAP-A: %dx%d ok=%v", w, h, ok)
	}

	for name, payload := range map[string][]byte{
		"empty":            nil,
		"pps":              pps,
		"idr slice":        {0x65, 0x88, 0x84},
		"fu-a":             {0x7c, 0x85, 0x00},
		"truncated sps":    sps[:5],
		"bad stap length":  {24, 0x00, 0x40, 0x67},
		"stap without sps": append([]byte{24, 0x00, byte(len(pps))}, pps...),
	} {
		if w, h, ok := spsResolutionInPayload(payload); ok {
			t.Errorf("%s: got %dx%d, want nothing", name, w, h)
		}
	}
}

func TestUnescapeRBSP(t *testing.T) {
	in := []byte{0x00, 0x00, 0x03, 0x01, 0x00, 0x00, 0x03, 0x00, 0x03}
	want := []byte{0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x03}
	if got := unescapeRBSP(in); string(got) != string(want) {
		t.Fatalf("got % x, want % x", got, want)
	}
}
