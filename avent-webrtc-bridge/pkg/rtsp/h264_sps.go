package rtsp

import "errors"

// The picture size an H.264 sequence parameter set declares (ITU-T H.264
// 7.3.2.1.1), so /status can say what resolution a stream really carries.
// Only the fields in front of the size and the cropping window are read; VUI
// and everything after it are not.

var errSPSShort = errors.New("sps: truncated")

// spsResolutionInPayload finds an SPS in an RTP payload, whole (NAL type 7) or
// inside a STAP-A (type 24), and returns the size it declares.
func spsResolutionInPayload(payload []byte) (width, height int, ok bool) {
	if len(payload) == 0 {
		return 0, 0, false
	}
	switch payload[0] & 0x1f {
	case 7:
		return spsResolution(payload)
	case 24: // STAP-A: a 16-bit size before each NAL
		for rest := payload[1:]; len(rest) >= 2; {
			size := int(rest[0])<<8 | int(rest[1])
			rest = rest[2:]
			if size == 0 || size > len(rest) {
				return 0, 0, false
			}
			if rest[0]&0x1f == 7 {
				return spsResolution(rest[:size])
			}
			rest = rest[size:]
		}
	}
	return 0, 0, false
}

// spsResolution is the cropped picture size of one SPS NAL unit, header
// byte included.
func spsResolution(nal []byte) (width, height int, ok bool) {
	if len(nal) < 4 || nal[0]&0x1f != 7 {
		return 0, 0, false
	}
	width, height, err := parseSPSSize(unescapeRBSP(nal[1:]))
	if err != nil || width <= 0 || height <= 0 {
		return 0, 0, false
	}
	return width, height, true
}

// unescapeRBSP drops the emulation prevention bytes: every 0x03 that follows
// two zero bytes.
func unescapeRBSP(b []byte) []byte {
	out := make([]byte, 0, len(b))
	zeros := 0
	for _, c := range b {
		if zeros >= 2 && c == 3 {
			zeros = 0
			continue
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

type bitReader struct {
	b   []byte
	pos int // in bits
}

func (r *bitReader) bit() (uint, error) {
	if r.pos >= len(r.b)*8 {
		return 0, errSPSShort
	}
	v := uint(r.b[r.pos/8]>>(7-r.pos%8)) & 1
	r.pos++
	return v, nil
}

func (r *bitReader) bits(n int) (uint, error) {
	var v uint
	for i := 0; i < n; i++ {
		b, err := r.bit()
		if err != nil {
			return 0, err
		}
		v = v<<1 | b
	}
	return v, nil
}

// ue is an unsigned Exp-Golomb code.
func (r *bitReader) ue() (uint, error) {
	zeros := 0
	for {
		b, err := r.bit()
		if err != nil {
			return 0, err
		}
		if b == 1 {
			break
		}
		zeros++
		if zeros > 31 {
			return 0, errors.New("sps: bad exp-golomb code")
		}
	}
	rest, err := r.bits(zeros)
	if err != nil {
		return 0, err
	}
	return 1<<zeros - 1 + rest, nil
}

// se is a signed Exp-Golomb code.
func (r *bitReader) se() (int, error) {
	v, err := r.ue()
	if err != nil {
		return 0, err
	}
	if v&1 == 1 {
		return int(v+1) / 2, nil
	}
	return -int(v / 2), nil
}

// highProfiles carry chroma format, bit depth and scaling matrices.
var highProfiles = map[uint]bool{
	100: true, 110: true, 122: true, 244: true, 44: true, 83: true,
	86: true, 118: true, 128: true, 138: true, 139: true, 134: true, 135: true,
}

func parseSPSSize(rbsp []byte) (width, height int, err error) {
	r := &bitReader{b: rbsp}
	profile, err := r.bits(8)
	if err != nil {
		return 0, 0, err
	}
	if _, err = r.bits(16); err != nil { // constraint flags, level_idc
		return 0, 0, err
	}
	if _, err = r.ue(); err != nil { // seq_parameter_set_id
		return 0, 0, err
	}

	chromaFormat := uint(1)
	separateColourPlane := uint(0)
	if highProfiles[profile] {
		if chromaFormat, err = r.ue(); err != nil {
			return 0, 0, err
		}
		if chromaFormat == 3 {
			if separateColourPlane, err = r.bit(); err != nil {
				return 0, 0, err
			}
		}
		for i := 0; i < 2; i++ { // bit depth luma, chroma
			if _, err = r.ue(); err != nil {
				return 0, 0, err
			}
		}
		if _, err = r.bit(); err != nil { // qpprime_y_zero_transform_bypass_flag
			return 0, 0, err
		}
		present, err := r.bit()
		if err != nil {
			return 0, 0, err
		}
		if present == 1 {
			lists := 8
			if chromaFormat == 3 {
				lists = 12
			}
			for i := 0; i < lists; i++ {
				listPresent, err := r.bit()
				if err != nil {
					return 0, 0, err
				}
				if listPresent == 1 {
					size := 16
					if i >= 6 {
						size = 64
					}
					if err = skipScalingList(r, size); err != nil {
						return 0, 0, err
					}
				}
			}
		}
	}

	if _, err = r.ue(); err != nil { // log2_max_frame_num_minus4
		return 0, 0, err
	}
	pocType, err := r.ue()
	if err != nil {
		return 0, 0, err
	}
	switch pocType {
	case 0:
		if _, err = r.ue(); err != nil { // log2_max_pic_order_cnt_lsb_minus4
			return 0, 0, err
		}
	case 1:
		if _, err = r.bit(); err != nil { // delta_pic_order_always_zero_flag
			return 0, 0, err
		}
		for i := 0; i < 2; i++ { // offset_for_non_ref_pic, offset_for_top_to_bottom_field
			if _, err = r.se(); err != nil {
				return 0, 0, err
			}
		}
		cycle, err := r.ue()
		if err != nil {
			return 0, 0, err
		}
		if cycle > 255 {
			return 0, 0, errors.New("sps: bad poc cycle")
		}
		for i := uint(0); i < cycle; i++ {
			if _, err = r.se(); err != nil {
				return 0, 0, err
			}
		}
	}
	if _, err = r.ue(); err != nil { // max_num_ref_frames
		return 0, 0, err
	}
	if _, err = r.bit(); err != nil { // gaps_in_frame_num_value_allowed_flag
		return 0, 0, err
	}
	widthMBs, err := r.ue()
	if err != nil {
		return 0, 0, err
	}
	heightMapUnits, err := r.ue()
	if err != nil {
		return 0, 0, err
	}
	frameMBsOnly, err := r.bit()
	if err != nil {
		return 0, 0, err
	}
	if frameMBsOnly == 0 {
		if _, err = r.bit(); err != nil { // mb_adaptive_frame_field_flag
			return 0, 0, err
		}
	}
	if _, err = r.bit(); err != nil { // direct_8x8_inference_flag
		return 0, 0, err
	}

	width = int(widthMBs+1) * 16
	height = int(2-frameMBsOnly) * int(heightMapUnits+1) * 16

	cropping, err := r.bit()
	if err != nil {
		return 0, 0, err
	}
	if cropping == 1 {
		var crop [4]uint // left, right, top, bottom
		for i := range crop {
			if crop[i], err = r.ue(); err != nil {
				return 0, 0, err
			}
		}
		// Table 6-1: the crop unit follows the chroma subsampling.
		cropX, cropY := 1, int(2-frameMBsOnly)
		if separateColourPlane == 0 && chromaFormat != 0 {
			subWidth, subHeight := 2, 1
			switch chromaFormat {
			case 1:
				subHeight = 2
			case 3:
				subWidth = 1
			}
			cropX, cropY = subWidth, subHeight*int(2-frameMBsOnly)
		}
		width -= int(crop[0]+crop[1]) * cropX
		height -= int(crop[2]+crop[3]) * cropY
	}
	return width, height, nil
}

func skipScalingList(r *bitReader, size int) error {
	last, next := 8, 8
	for j := 0; j < size; j++ {
		if next != 0 {
			delta, err := r.se()
			if err != nil {
				return err
			}
			next = (last + delta + 256) % 256
		}
		if next != 0 {
			last = next
		}
	}
	return nil
}
