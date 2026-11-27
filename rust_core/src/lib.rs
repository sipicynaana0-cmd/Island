//! Native Rust High-Performance Core v2.2 for Island (Dynamic Island for Windows)
//! Zero external crate dependencies — pure std + direct Win32 FFI.
//! Exports:
//!   - rust_core_version() -> u32 (220)
//!   - rust_fft_32bands(...) -> fast Radix-2 FFT + 32 logarithmic bands + AGC
//!   - rust_is_fullscreen_game(own_hwnd) -> Win32 fullscreen game detector
//!   - rust_get_system_stats(out_cpu, out_ram) -> Win32 GetSystemTimes + GlobalMemoryStatusEx
//!   - rust_compute_wave_offsets(...) -> fast audio-reactive ferrofluid wave solver
//!   - rust_step_springs(...) -> batched sub-stepped spring physics solver (60..240 Hz)
//!   - rust_get_refresh_rate_hz() -> native Win32 display Hz detector
//!   - rust_clipboard_seq() -> 1ns Win32 GetClipboardSequenceNumber
//!   - rust_poll_hotkeys() -> Win32 edge-triggered hotkeys (Ctrl+Shift+I, Alt+M, Alt+Space)
//!   - rust_find_lyric_index(...) -> O(log N) synced LRC line lookup
//!   - rust_get_capslock_state() -> native CapsLock indicator
//!   - rust_get_removable_drive_mask() -> native USB/removable drive detector
//!   - rust_extract_file_icon_rgba(...) -> native Win32 SHGetFileInfoW + DrawIconEx icon extractor
//!   - rust_poll_steam_download(...) -> native multi-library Steam live download scanner
//!   - rust_eval_math_expr(...) -> safe recursive-descent calculator for Spotlight

use std::f32::consts::PI;
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::Instant;

const N_FFT: usize = 2048;
const N_BANDS: usize = 32;

// -----------------------------------------------------------------------------
// Win32 FFI Bindings (Zero external crates!)
// -----------------------------------------------------------------------------
#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Rect {
    left: i32,
    top: i32,
    right: i32,
    bottom: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MonitorInfo {
    cb_size: u32,
    rc_monitor: Rect,
    rc_work: Rect,
    dw_flags: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct FileTime {
    low: u32,
    high: u32,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MemoryStatusEx {
    dw_length: u32,
    dw_memory_load: u32,
    ull_total_phys: u64,
    ull_avail_phys: u64,
    ull_total_page_file: u64,
    ull_avail_page_file: u64,
    ull_total_virtual: u64,
    ull_avail_virtual: u64,
    ull_avail_extended_virtual: u64,
}

#[repr(C)]
struct DevModeW {
    dm_device_name: [u16; 32],
    dm_spec_version: u16,
    dm_driver_version: u16,
    dm_size: u16,
    dm_driver_extra: u16,
    dm_fields: u32,
    dm_pos_x: i32,
    dm_pos_y: i32,
    dm_display_orientation: u32,
    dm_display_fixed_output: u32,
    dm_color: i16,
    dm_duplex: i16,
    dm_y_resolution: i16,
    dm_tt_option: i16,
    dm_collate: i16,
    dm_form_name: [u16; 32],
    dm_log_pixels: u16,
    dm_bits_per_pel: u32,
    dm_pels_width: u32,
    dm_pels_height: u32,
    dm_display_flags: u32,
    dm_display_frequency: u32,
    dm_icm_method: u32,
    dm_icm_intent: u32,
    dm_media_type: u32,
    dm_dither_type: u32,
    dm_reserved1: u32,
    dm_reserved2: u32,
    dm_panning_width: u32,
    dm_panning_height: u32,
}

#[repr(C)]
struct ShFileInfoW {
    h_icon: usize,
    i_icon: i32,
    dw_attributes: u32,
    sz_display_name: [u16; 260],
    sz_type_name: [u16; 80],
}

#[repr(C)]
struct BitmapInfoHeader {
    bi_size: u32,
    bi_width: i32,
    bi_height: i32,
    bi_planes: u16,
    bi_bit_count: u16,
    bi_compression: u32,
    bi_size_image: u32,
    bi_x_pels_per_meter: i32,
    bi_y_pels_per_meter: i32,
    bi_clr_used: u32,
    bi_clr_important: u32,
}

#[repr(C)]
struct BitmapInfo {
    bmi_header: BitmapInfoHeader,
    bmi_colors: [u32; 1],
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct Point {
    x: i32,
    y: i32,
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct SystemPowerStatus {
    ac_line_status: u8,
    battery_flag: u8,
    battery_life_percent: u8,
    system_status_flag: u8,
    battery_life_time: u32,
    battery_full_life_time: u32,
}

#[link(name = "user32")]
extern "system" {
    fn GetForegroundWindow() -> usize;
    fn GetWindowThreadProcessId(hwnd: usize, lpdw_process_id: *mut u32) -> u32;
    fn GetKeyboardLayout(id_thread: u32) -> usize;
    fn GetCursorPos(lp_point: *mut Point) -> i32;
    fn IsWindowVisible(hwnd: usize) -> i32;
    fn IsIconic(hwnd: usize) -> i32;
    fn GetWindowRect(hwnd: usize, lp_rect: *mut Rect) -> i32;
    fn MonitorFromWindow(hwnd: usize, dw_flags: u32) -> usize;
    fn GetMonitorInfoW(h_monitor: usize, lpmi: *mut MonitorInfo) -> i32;
    fn GetClassNameW(hwnd: usize, lp_class_name: *mut u16, n_max_count: i32) -> i32;
    fn GetWindowLongW(hwnd: usize, n_index: i32) -> i32;
    fn EnumDisplaySettingsW(lpsz_device_name: *const u16, i_mode_num: u32, lp_dev_mode: *mut DevModeW) -> i32;
    fn GetClipboardSequenceNumber() -> u32;
    fn GetAsyncKeyState(v_key: i32) -> i16;
    fn GetKeyState(v_key: i32) -> i16;
    fn DestroyIcon(h_icon: usize) -> i32;
    fn DrawIconEx(
        hdc: usize,
        x_left: i32,
        y_top: i32,
        h_icon: usize,
        cx_width: i32,
        cy_width: i32,
        istep_if_ani_cur: u32,
        hbr_flicker_free_draw: usize,
        di_flags: u32,
    ) -> i32;
    fn GetDC(hwnd: usize) -> usize;
    fn ReleaseDC(hwnd: usize, hdc: usize) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn GetSystemTimes(idle: *mut FileTime, kernel: *mut FileTime, user: *mut FileTime) -> i32;
    fn GlobalMemoryStatusEx(lp_buffer: *mut MemoryStatusEx) -> i32;
    fn GetLogicalDrives() -> u32;
    fn GetDriveTypeW(lp_root_path_name: *const u16) -> u32;
    fn GetSystemPowerStatus(lp_system_power_status: *mut SystemPowerStatus) -> i32;
}

#[link(name = "shell32")]
extern "system" {
    fn SHGetFileInfoW(
        psz_path: *const u16,
        dw_file_attributes: u32,
        psfi: *mut ShFileInfoW,
        cb_file_info: u32,
        u_flags: u32,
    ) -> usize;
}

#[link(name = "advapi32")]
extern "system" {
    fn RegOpenKeyExW(
        h_key: isize,
        lp_sub_key: *const u16,
        ul_options: u32,
        sam_desired: u32,
        phk_result: *mut usize,
    ) -> i32;
    fn RegQueryValueExW(
        h_key: usize,
        lp_value_name: *const u16,
        lp_reserved: *mut u32,
        lp_type: *mut u32,
        lp_data: *mut u8,
        lpcb_data: *mut u32,
    ) -> i32;
    fn RegCloseKey(h_key: usize) -> i32;
}

#[link(name = "gdi32")]
extern "system" {
    fn CreateCompatibleDC(hdc: usize) -> usize;
    fn DeleteDC(hdc: usize) -> i32;
    fn SelectObject(hdc: usize, h_obj: usize) -> usize;
    fn DeleteObject(h_obj: usize) -> i32;
    fn GetPixel(hdc: usize, x: i32, y: i32) -> u32;
    fn CreateDIBSection(
        hdc: usize,
        pbmi: *const BitmapInfo,
        usage: u32,
        ppv_bits: *mut *mut u8,
        h_section: usize,
        offset: u32,
    ) -> usize;
}

// -----------------------------------------------------------------------------
// Persistent FFT & Telemetry State (Zero per-frame heap allocations!)
// -----------------------------------------------------------------------------
struct FftEngine {
    hann: [f32; N_FFT],
    re: [f32; N_FFT],
    im: [f32; N_FFT],
    ring: [f32; N_FFT],
    agc_db: f32,
    last_sr: f32,
    bin_lo: [usize; N_BANDS],
    bin_hi: [usize; N_BANDS],
    tilt: [f32; N_BANDS],
}

impl FftEngine {
    fn new() -> Self {
        let mut hann = [0.0f32; N_FFT];
        for i in 0..N_FFT {
            hann[i] = 0.5 * (1.0 - (2.0 * PI * (i as f32) / ((N_FFT - 1) as f32)).cos());
        }
        let mut eng = Self {
            hann,
            re: [0.0; N_FFT],
            im: [0.0; N_FFT],
            ring: [0.0; N_FFT],
            agc_db: -28.0,
            last_sr: 0.0,
            bin_lo: [1; N_BANDS],
            bin_hi: [2; N_BANDS],
            tilt: [0.0; N_BANDS],
        };
        eng.rebuild_bins(48000.0);
        eng
    }

    fn rebuild_bins(&mut self, sr: f32) {
        if (sr - self.last_sr).abs() < 1.0 || sr < 8000.0 {
            return;
        }
        self.last_sr = sr;
        let f_min = 40.0f32;
        let f_max = (sr * 0.45).min(16000.0);
        let ratio = (f_max / f_min).powf(1.0 / (N_BANDS as f32));
        let bin_hz = sr / (N_FFT as f32);

        let mut f_lo = f_min;
        for i in 0..N_BANDS {
            let f_hi = f_lo * ratio;
            let lo = ((f_lo / bin_hz).floor() as usize).clamp(1, N_FFT / 2 - 2);
            let hi = ((f_hi / bin_hz).ceil() as usize).clamp(lo + 1, N_FFT / 2 - 1);
            self.bin_lo[i] = lo;
            self.bin_hi[i] = hi;
            let center = (f_lo * f_hi).sqrt();
            self.tilt[i] = 3.5 * (center / 250.0).log2();
            f_lo = f_hi;
        }
    }

    /// In-place Radix-2 Cooley-Tukey FFT on self.re / self.im (N_FFT = 2048)
    fn run_fft(&mut self) {
        // Bit-reversal permutation
        let mut j = 0usize;
        for i in 1..N_FFT {
            let mut bit = N_FFT >> 1;
            while j & bit != 0 {
                j ^= bit;
                bit >>= 1;
            }
            j ^= bit;
            if i < j {
                self.re.swap(i, j);
                self.im.swap(i, j);
            }
        }

        // Butterfly stages
        let mut len = 2usize;
        while len <= N_FFT {
            let ang = -2.0 * PI / (len as f32);
            let wlen_re = ang.cos();
            let wlen_im = ang.sin();
            let half = len >> 1;

            let mut i = 0usize;
            while i < N_FFT {
                let mut w_re = 1.0f32;
                let mut w_im = 0.0f32;
                for k in 0..half {
                    let u_re = self.re[i + k];
                    let u_im = self.im[i + k];
                    let v_re = self.re[i + k + half] * w_re - self.im[i + k + half] * w_im;
                    let v_im = self.re[i + k + half] * w_im + self.im[i + k + half] * w_re;

                    self.re[i + k] = u_re + v_re;
                    self.im[i + k] = u_im + v_im;
                    self.re[i + k + half] = u_re - v_re;
                    self.im[i + k + half] = u_im - v_im;

                    let next_w_re = w_re * wlen_re - w_im * wlen_im;
                    w_im = w_re * wlen_im + w_im * wlen_re;
                    w_re = next_w_re;
                }
                i += len;
            }
            len <<= 1;
        }
    }

    fn process(&mut self, samples: &[f32], sr: f32, out_bands: &mut [f32; N_BANDS]) -> f32 {
        self.rebuild_bins(sr);

        let n = samples.len().min(N_FFT);
        if n > 0 {
            if n < N_FFT {
                self.ring.copy_within(n..N_FFT, 0);
                self.ring[(N_FFT - n)..N_FFT].copy_from_slice(&samples[samples.len() - n..]);
            } else {
                self.ring.copy_from_slice(&samples[samples.len() - N_FFT..]);
            }
        }

        // Compute RMS on incoming slice
        let mut sum_sq = 0.0f32;
        for &s in samples {
            sum_sq += s * s;
        }
        let rms = if !samples.is_empty() {
            (sum_sq / (samples.len() as f32)).sqrt()
        } else {
            0.0
        };

        if rms < 2.0e-5 {
            out_bands.fill(0.0);
            return 0.0;
        }

        for i in 0..N_FFT {
            self.re[i] = self.ring[i] * self.hann[i];
            self.im[i] = 0.0;
        }

        self.run_fft();

        let mut db = [0.0f32; N_BANDS];
        let mut top = -120.0f32;
        for b in 0..N_BANDS {
            let lo = self.bin_lo[b];
            let hi = self.bin_hi[b];
            let mut acc = 0.0f32;
            for k in lo..hi {
                acc += self.re[k] * self.re[k] + self.im[k] * self.im[k];
            }
            let mean_p = acc / ((hi - lo).max(1) as f32);
            let val_db = 10.0 * (mean_p + 1e-12).log10() + self.tilt[b];
            db[b] = val_db;
            if val_db > top {
                top = val_db;
            }
        }

        if top > self.agc_db {
            self.agc_db = top;
        } else {
            self.agc_db += (top - self.agc_db) * 0.012;
        }

        let rng = 42.0f32;
        let floor = self.agc_db - rng;
        for b in 0..N_BANDS {
            let v = ((db[b] - floor) / rng).clamp(0.0, 1.0);
            out_bands[b] = v.powf(1.35);
        }

        let level_db = 20.0 * (rms + 1e-12).log10();
        ((level_db + 48.0) / 40.0).clamp(0.0, 1.0)
    }
}

struct CpuState {
    prev_idle: u64,
    prev_total: u64,
    last_cpu: f32,
}

static FFT_STATE: Mutex<Option<FftEngine>> = Mutex::new(None);
static CPU_STATE: Mutex<CpuState> = Mutex::new(CpuState {
    prev_idle: 0,
    prev_total: 0,
    last_cpu: 12.0,
});

#[inline]
fn ft_to_u64(ft: FileTime) -> u64 {
    ((ft.high as u64) << 32) | (ft.low as u64)
}

// -----------------------------------------------------------------------------
// Exported C-ABI Functions
// -----------------------------------------------------------------------------
#[no_mangle]
pub extern "C" fn rust_core_version() -> u32 {
    231
}

#[no_mangle]
pub unsafe extern "C" fn rust_fft_32bands(
    samples_ptr: *const f32,
    n_samples: usize,
    sample_rate: f32,
    out_bands: *mut f32,
    out_level: *mut f32,
) -> i32 {
    if samples_ptr.is_null() || out_bands.is_null() || out_level.is_null() {
        return 0;
    }
    let samples = std::slice::from_raw_parts(samples_ptr, n_samples);
    let mut guard = match FFT_STATE.lock() {
        Ok(g) => g,
        Err(_) => return 0,
    };
    let eng = guard.get_or_insert_with(FftEngine::new);
    let mut bands = [0.0f32; N_BANDS];
    let lvl = eng.process(samples, sample_rate, &mut bands);
    let out_slice = std::slice::from_raw_parts_mut(out_bands, N_BANDS);
    out_slice.copy_from_slice(&bands);
    *out_level = lvl;
    1
}

/// Detects whether a fullscreen game or borderless fullscreen application is active.
/// Ignores Desktop (Progman / WorkerW), Taskbar (Shell_TrayWnd), and our own Island HWND.
#[no_mangle]
pub extern "C" fn rust_is_fullscreen_game(own_hwnd: usize) -> i32 {
    unsafe {
        let fg = GetForegroundWindow();
        if fg == 0 || fg == own_hwnd {
            return 0;
        }
        if IsWindowVisible(fg) == 0 || IsIconic(fg) != 0 {
            return 0;
        }

        // Exclude Windows desktop shell classes
        let mut cls_buf = [0u16; 64];
        let cls_len = GetClassNameW(fg, cls_buf.as_mut_ptr(), 63);
        if cls_len > 0 {
            let cls = String::from_utf16_lossy(&cls_buf[..cls_len as usize]);
            if matches!(
                cls.as_str(),
                "Progman"
                    | "WorkerW"
                    | "Shell_TrayWnd"
                    | "Shell_SecondaryTrayWnd"
                    | "Windows.UI.Core.CoreWindow"
                    | "MultitaskingViewFrame"
                    | "XamlExplorerHostIslandWindow"
            ) {
                return 0;
            }
        }

        let mut wr = Rect::default();
        if GetWindowRect(fg, &mut wr) == 0 {
            return 0;
        }
        let hmon = MonitorFromWindow(fg, 2); // MONITOR_DEFAULTTONEAREST
        if hmon == 0 {
            return 0;
        }
        let mut mi = MonitorInfo {
            cb_size: std::mem::size_of::<MonitorInfo>() as u32,
            rc_monitor: Rect::default(),
            rc_work: Rect::default(),
            dw_flags: 0,
        };
        if GetMonitorInfoW(hmon, &mut mi) == 0 {
            return 0;
        }

        let mon = mi.rc_monitor;
        let covers_monitor = wr.left <= mon.left + 2
            && wr.top <= mon.top + 2
            && wr.right >= mon.right - 2
            && wr.bottom >= mon.bottom - 2;

        if !covers_monitor {
            return 0;
        }

        // Distinguish true fullscreen / borderless game from normal maximized desktop window (like Chrome/Discord)
        // A normal maximized desktop app has WS_CAPTION (0x00C00000) & WS_THICKFRAME (0x00040000) or leaves taskbar visible.
        // Fullscreen games & F11 fullscreen video cover the entire monitor including taskbar area without WS_CAPTION or WS_DLGFRAME.
        const GWL_STYLE: i32 = -16;
        const WS_CAPTION: i32 = 0x00C00000;
        const WS_POPUP: i32 = -2147483648i32; // 0x80000000
        let style = GetWindowLongW(fg, GWL_STYLE);
        let is_borderless_or_exclusive = (style & WS_CAPTION) != WS_CAPTION || (style & WS_POPUP) != 0;

        // Also check if window covers the taskbar (rc_work != rc_monitor and wr covers full rc_monitor)
        let covers_taskbar = mi.rc_work.bottom < mon.bottom && wr.bottom >= mon.bottom - 1;

        if is_borderless_or_exclusive || covers_taskbar {
            1
        } else {
            0
        }
    }
}

#[no_mangle]
pub unsafe extern "C" fn rust_get_system_stats(out_cpu: *mut f32, out_ram: *mut f32) -> i32 {
    if out_cpu.is_null() || out_ram.is_null() {
        return 0;
    }
    let mut idle = FileTime::default();
    let mut kernel = FileTime::default();
    let mut user = FileTime::default();
    if GetSystemTimes(&mut idle, &mut kernel, &mut user) != 0 {
        let i_now = ft_to_u64(idle);
        let t_now = ft_to_u64(kernel).saturating_add(ft_to_u64(user));
        if let Ok(mut st) = CPU_STATE.lock() {
            if st.prev_total > 0 && t_now > st.prev_total {
                let di = i_now.saturating_sub(st.prev_idle);
                let dt = t_now.saturating_sub(st.prev_total);
                let busy = dt.saturating_sub(di) as f32 / (dt as f32);
                st.last_cpu = (busy * 100.0).clamp(0.0, 100.0);
            }
            st.prev_idle = i_now;
            st.prev_total = t_now;
            *out_cpu = st.last_cpu;
        }
    }

    let mut ms = MemoryStatusEx {
        dw_length: std::mem::size_of::<MemoryStatusEx>() as u32,
        dw_memory_load: 0,
        ull_total_phys: 0,
        ull_avail_phys: 0,
        ull_total_page_file: 0,
        ull_avail_page_file: 0,
        ull_total_virtual: 0,
        ull_avail_virtual: 0,
        ull_avail_extended_virtual: 0,
    };
    if GlobalMemoryStatusEx(&mut ms) != 0 {
        *out_ram = ms.dw_memory_load as f32;
    }
    1
}

/// Fast native Rust audio-reactive bottom wave solver (0 heap allocations)
#[no_mangle]
pub unsafe extern "C" fn rust_compute_wave_offsets(
    bands_ptr: *const f32,
    n_bands: usize,
    peak: f32,
    phase: f32,
    amp_max: f32,
    active_gate: f32,
    n_pts: usize,
    out_dy: *mut f32,
) {
    if bands_ptr.is_null() || out_dy.is_null() || n_bands == 0 || n_pts < 2 {
        return;
    }
    let bands = std::slice::from_raw_parts(bands_ptr, n_bands);
    let out = std::slice::from_raw_parts_mut(out_dy, n_pts);
    let denom = (n_pts - 1) as f32;
    let max_idx = (n_bands - 1) as f32;

    for i in 0..n_pts {
        let t = (i as f32) / denom;
        let env = (t * PI).sin().max(0.0).powf(0.8);
        let d = (t - 0.5).abs() * 2.0;
        let fi = d * max_idx * 0.78;
        let i0 = (fi as usize).min(n_bands - 1);
        let i1 = (i0 + 1).min(n_bands - 1);
        let fr = fi - (i0 as f32);
        let b_val = (bands[i0] * (1.0 - fr) + bands[i1] * fr) * active_gate;
        let shimmer = (t * PI * 7.0 + phase).sin() * (b_val * 1.4 + peak * 0.8) * active_gate;
        out[i] = (b_val * amp_max + shimmer) * env;
    }
}

/// High-precision batched sub-stepped spring physics solver (unconditionally stable 60..240 Hz!)
/// Processes all UI springs in a single C-ABI call (< 2 microseconds).
#[no_mangle]
pub unsafe extern "C" fn rust_step_springs(
    vals: *mut f32,
    vels: *mut f32,
    targets: *const f32,
    stiffs: *const f32,
    damps: *const f32,
    count: usize,
    dt: f32,
) -> i32 {
    if vals.is_null() || vels.is_null() || targets.is_null() || stiffs.is_null() || damps.is_null() || count == 0 {
        return 0;
    }
    let dt_c = dt.clamp(0.0005, 0.045);
    let sub_steps: usize = if dt_c > 0.012 { 3 } else { 2 };
    let h = dt_c / (sub_steps as f32);

    let v_s = std::slice::from_raw_parts_mut(vals, count);
    let vel_s = std::slice::from_raw_parts_mut(vels, count);
    let t_s = std::slice::from_raw_parts(targets, count);
    let k_s = std::slice::from_raw_parts(stiffs, count);
    let d_s = std::slice::from_raw_parts(damps, count);

    let mut active_count = 0i32;
    for i in 0..count {
        let mut x = v_s[i];
        let mut v = vel_s[i];
        let tgt = t_s[i];
        let k = k_s[i];
        let c = d_s[i];

        if (tgt - x).abs() < 0.0004 && v.abs() < 0.0004 {
            v_s[i] = tgt;
            vel_s[i] = 0.0;
            continue;
        }
        for _ in 0..sub_steps {
            let acc = (tgt - x) * k - v * c;
            v += acc * h;
            x += v * h;
        }
        if (tgt - x).abs() < 0.0004 && v.abs() < 0.0004 {
            v_s[i] = tgt;
            vel_s[i] = 0.0;
        } else {
            v_s[i] = x;
            vel_s[i] = v;
            active_count += 1;
        }
    }
    active_count
}

/// Detects the native refresh rate (Hz) of the primary Windows display (e.g. 60, 120, 144, 165, 240).
#[no_mangle]
pub extern "C" fn rust_get_refresh_rate_hz() -> u32 {
    unsafe {
        let mut dm: DevModeW = std::mem::zeroed();
        dm.dm_size = std::mem::size_of::<DevModeW>() as u16;
        const ENUM_CURRENT_SETTINGS: u32 = 0xFFFF_FFFF;
        if EnumDisplaySettingsW(std::ptr::null(), ENUM_CURRENT_SETTINGS, &mut dm) != 0 {
            let hz = dm.dm_display_frequency;
            if (30..=360).contains(&hz) {
                return hz;
            }
        }
        60
    }
}

/// 1-nanosecond Win32 clipboard sequence counter (avoids heavy Qt clipboard polling).
#[no_mangle]
pub extern "C" fn rust_clipboard_seq() -> u32 {
    unsafe { GetClipboardSequenceNumber() }
}

static PREV_HOTKEY_MASK: AtomicU32 = AtomicU32::new(0);

/// Edge-triggered Win32 global hotkey poller (zero system hook overhead):
///   bit 0 (1): Ctrl + Shift + I -> Summon / toggle Island with 2-Eyes reveal
///   bit 1 (2): Alt + M -> Instant audio mute toggle
///   bit 2 (4): Alt + Space -> Open / close Island Spotlight Search
#[no_mangle]
pub extern "C" fn rust_poll_hotkeys() -> u32 {
    unsafe {
        let ctrl = (GetAsyncKeyState(0x11) as u16 & 0x8000) != 0;
        let shift = (GetAsyncKeyState(0x10) as u16 & 0x8000) != 0;
        let alt = (GetAsyncKeyState(0x12) as u16 & 0x8000) != 0;
        let key_i = (GetAsyncKeyState(0x49) as u16 & 0x8000) != 0; // 'I'
        let key_m = (GetAsyncKeyState(0x4D) as u16 & 0x8000) != 0; // 'M'
        let key_sp = (GetAsyncKeyState(0x20) as u16 & 0x8000) != 0; // VK_SPACE

        let mut curr = 0u32;
        if ctrl && shift && key_i {
            curr |= 1;
        }
        if alt && !ctrl && !shift && key_m {
            curr |= 2;
        }
        if alt && !ctrl && !shift && key_sp {
            curr |= 4;
        }
        let prev = PREV_HOTKEY_MASK.swap(curr, Ordering::Relaxed);
        curr & !prev
    }
}

/// O(log N) binary search over sorted LRC lyric timestamps (seconds).
/// Returns active line index, or -1 if before the first timestamp.
#[no_mangle]
pub unsafe extern "C" fn rust_find_lyric_index(
    timestamps: *const f32,
    count: usize,
    pos_sec: f32,
) -> i32 {
    if timestamps.is_null() || count == 0 {
        return -1;
    }
    let ts = std::slice::from_raw_parts(timestamps, count);
    if pos_sec < ts[0] {
        return -1;
    }
    let mut lo = 0usize;
    let mut hi = count;
    while lo + 1 < hi {
        let mid = (lo + hi) >> 1;
        if ts[mid] <= pos_sec {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    lo as i32
}

/// Native Win32 CapsLock toggle state (1 = ON, 0 = OFF).
#[no_mangle]
pub extern "C" fn rust_get_capslock_state() -> i32 {
    unsafe { (GetKeyState(0x14) & 1) as i32 }
}

/// Native Win32 bitmask of mounted USB / Removable drives (A..Z -> bits 0..25).
#[no_mangle]
pub extern "C" fn rust_get_removable_drive_mask() -> u32 {
    unsafe {
        let mask = GetLogicalDrives();
        let mut out = 0u32;
        for i in 0..26u32 {
            if (mask & (1 << i)) != 0 {
                let root: [u16; 4] = [(b'A' as u16) + (i as u16), b':' as u16, b'\\' as u16, 0];
                // DRIVE_REMOVABLE = 2
                if GetDriveTypeW(root.as_ptr()) == 2 {
                    out |= 1 << i;
                }
            }
        }
        out
    }
}

/// Extracts the real Windows high-res icon of any .exe / .lnk / file into RGBA8888 (size * size * 4 bytes).
/// Uses SHGetFileInfoW + top-down 32-bit CreateDIBSection + DrawIconEx (handles full alpha cleanly).
#[no_mangle]
pub unsafe extern "C" fn rust_extract_file_icon_rgba(
    path_utf16: *const u16,
    out_rgba: *mut u8,
    size: u32,
) -> i32 {
    if path_utf16.is_null() || out_rgba.is_null() || size < 16 || size > 128 {
        return 0;
    }
    const SHGFI_ICON: u32 = 0x0000_0100;
    const SHGFI_LARGEICON: u32 = 0x0000_0000;
    const DI_NORMAL: u32 = 0x0003;

    let mut sfi: ShFileInfoW = std::mem::zeroed();
    let res = SHGetFileInfoW(
        path_utf16,
        0,
        &mut sfi,
        std::mem::size_of::<ShFileInfoW>() as u32,
        SHGFI_ICON | SHGFI_LARGEICON,
    );
    if res == 0 || sfi.h_icon == 0 {
        return 0;
    }

    let sz = size as i32;
    let screen_dc = GetDC(0);
    let mem_dc = CreateCompatibleDC(screen_dc);
    if mem_dc == 0 {
        ReleaseDC(0, screen_dc);
        DestroyIcon(sfi.h_icon);
        return 0;
    }

    let bmi = BitmapInfo {
        bmi_header: BitmapInfoHeader {
            bi_size: std::mem::size_of::<BitmapInfoHeader>() as u32,
            bi_width: sz,
            bi_height: -sz, // top-down DIB
            bi_planes: 1,
            bi_bit_count: 32,
            bi_compression: 0, // BI_RGB
            bi_size_image: 0,
            bi_x_pels_per_meter: 0,
            bi_y_pels_per_meter: 0,
            bi_clr_used: 0,
            bi_clr_important: 0,
        },
        bmi_colors: [0],
    };

    let mut bits_ptr: *mut u8 = std::ptr::null_mut();
    let hbm = CreateDIBSection(mem_dc, &bmi, 0, &mut bits_ptr, 0, 0);
    if hbm == 0 || bits_ptr.is_null() {
        DeleteDC(mem_dc);
        ReleaseDC(0, screen_dc);
        DestroyIcon(sfi.h_icon);
        return 0;
    }

    let n_bytes = (size as usize) * (size as usize) * 4;
    std::ptr::write_bytes(bits_ptr, 0, n_bytes);
    let old_obj = SelectObject(mem_dc, hbm);
    let ok_draw = DrawIconEx(mem_dc, 0, 0, sfi.h_icon, sz, sz, 0, 0, DI_NORMAL);

    let mut success = 0i32;
    if ok_draw != 0 {
        let src = std::slice::from_raw_parts(bits_ptr, n_bytes);
        let dst = std::slice::from_raw_parts_mut(out_rgba, n_bytes);
        let mut any_alpha = false;
        let mut any_color = false;
        for px in (0..n_bytes).step_by(4) {
            let b = src[px];
            let g = src[px + 1];
            let r = src[px + 2];
            let a = src[px + 3];
            dst[px] = r;
            dst[px + 1] = g;
            dst[px + 2] = b;
            dst[px + 3] = a;
            if a > 0 {
                any_alpha = true;
            }
            if r > 0 || g > 0 || b > 0 {
                any_color = true;
            }
        }
        // Handle legacy non-alpha icons where RGB was drawn with alpha=0
        if !any_alpha && any_color {
            for px in (0..n_bytes).step_by(4) {
                if dst[px] > 0 || dst[px + 1] > 0 || dst[px + 2] > 0 {
                    dst[px + 3] = 255;
                }
            }
        }
        if any_alpha || any_color {
            success = 1;
        }
    }

    SelectObject(mem_dc, old_obj);
    DeleteObject(hbm);
    DeleteDC(mem_dc);
    ReleaseDC(0, screen_dc);
    DestroyIcon(sfi.h_icon);
    success
}

// -----------------------------------------------------------------------------
// Native Accurate Multi-Library Steam Download Scanner v2.3 (Registry + content_log + ACF)
// -----------------------------------------------------------------------------
struct SteamDlTrack {
    last_app: String,
    last_acf_bytes: u64,
    est_bytes: f64,
    last_acf_time: Option<Instant>,
    last_tick: Option<Instant>,
    smooth_mbps: f32,
}

static STEAM_DL_STATE: Mutex<SteamDlTrack> = Mutex::new(SteamDlTrack {
    last_app: String::new(),
    last_acf_bytes: 0,
    est_bytes: 0.0,
    last_acf_time: None,
    last_tick: None,
    smooth_mbps: 0.0,
});

fn read_steam_root_from_registry() -> Option<PathBuf> {
    unsafe {
        const HKEY_CURRENT_USER: isize = -2147483647; // 0x80000001
        const KEY_READ: u32 = 0x20019;
        let subkey: Vec<u16> = "Software\\Valve\\Steam\0".encode_utf16().collect();
        let val_name: Vec<u16> = "SteamPath\0".encode_utf16().collect();
        let mut hkey: usize = 0;
        if RegOpenKeyExW(HKEY_CURRENT_USER, subkey.as_ptr(), 0, KEY_READ, &mut hkey) != 0 {
            return None;
        }
        let mut buf = [0u16; 260];
        let mut cb_data = (buf.len() * 2) as u32;
        let mut reg_type = 0u32;
        let ok = RegQueryValueExW(
            hkey,
            val_name.as_ptr(),
            std::ptr::null_mut(),
            &mut reg_type,
            buf.as_mut_ptr() as *mut u8,
            &mut cb_data,
        );
        RegCloseKey(hkey);
        if ok == 0 && cb_data >= 2 {
            let n_u16 = (cb_data as usize / 2).min(buf.len());
            let s = String::from_utf16_lossy(&buf[..n_u16])
                .trim_end_matches('\0')
                .replace('/', "\\");
            let p = PathBuf::from(s);
            if p.is_dir() {
                return Some(p);
            }
        }
        None
    }
}

fn extract_vdf_field<'a>(content: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("\"{}\"", key);
    for line in content.lines() {
        let tr = line.trim();
        if let Some(rest) = tr.strip_prefix(&needle) {
            let rest = rest.trim();
            if let Some(after_q) = rest.strip_prefix('"') {
                if let Some(end_q) = after_q.find('"') {
                    return Some(&after_q[..end_q]);
                }
            }
        }
    }
    None
}

fn collect_steam_roots_and_apps() -> (Option<PathBuf>, Vec<PathBuf>) {
    let mut dirs = Vec::new();
    let reg_root = read_steam_root_from_registry();
    let mut roots: Vec<PathBuf> = Vec::new();
    if let Some(ref r) = reg_root {
        roots.push(r.clone());
    }
    for d in &[
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        r"D:\Steam",
        r"E:\Steam",
    ] {
        let p = PathBuf::from(d);
        if p.is_dir() && !roots.iter().any(|x| x.to_string_lossy().eq_ignore_ascii_case(&p.to_string_lossy())) {
            roots.push(p);
        }
    }

    let primary_root = roots.first().cloned();
    for r in &roots {
        let sa = r.join("steamapps");
        if sa.is_dir() && !dirs.iter().any(|x: &PathBuf| x.to_string_lossy().eq_ignore_ascii_case(&sa.to_string_lossy())) {
            dirs.push(sa.clone());
        }
        let vdf = sa.join("libraryfolders.vdf");
        if let Ok(txt) = fs::read_to_string(&vdf) {
            for line in txt.lines() {
                if let Some(raw_path) = extract_vdf_field(line, "path") {
                    let clean = raw_path.replace("\\\\", "\\");
                    let lib_sa = Path::new(&clean).join("steamapps");
                    if lib_sa.is_dir()
                        && !dirs
                            .iter()
                            .any(|x: &PathBuf| x.to_string_lossy().eq_ignore_ascii_case(&lib_sa.to_string_lossy()))
                    {
                        dirs.push(lib_sa);
                    }
                }
            }
        }
    }
    for extra in &[r"D:\SteamLibrary\steamapps", r"E:\SteamLibrary\steamapps"] {
        let p = PathBuf::from(extra);
        if p.is_dir() && !dirs.iter().any(|x| x.to_string_lossy().eq_ignore_ascii_case(&p.to_string_lossy())) {
            dirs.push(p);
        }
    }
    (primary_root, dirs)
}

/// Parses the tail of Steam's logs/content_log.txt for the REAL active AppID and REAL live speed (MB/s).
/// Returns (active_appid, live_mb_per_sec, is_actively_running).
fn parse_steam_content_log(steam_root: &Path) -> (Option<String>, f32, bool) {
    let log_path = steam_root.join("logs").join("content_log.txt");
    let mut f = match File::open(&log_path) {
        Ok(file) => file,
        Err(_) => return (None, 0.0, false),
    };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let read_sz = len.min(24_576);
    if len > read_sz {
        let _ = f.seek(SeekFrom::End(-(read_sz as i64)));
    }
    let mut buf = Vec::with_capacity(read_sz as usize);
    if f.read_to_end(&mut buf).is_err() {
        return (None, 0.0, false);
    }
    let text = String::from_utf8_lossy(&buf);

    let mut active_app: Option<String> = None;
    let mut live_mb_s: f32 = 0.0;
    let mut is_running = false;

    for line in text.lines() {
        // Parse exact download rate lines from Steam:
        // "Current download rate: 48.210 Mbps"
        if let Some(pos) = line.find("Current download rate:") {
            let rest = line[pos + "Current download rate:".len()..].trim();
            if let Some(num_str) = rest.split_whitespace().next() {
                if let Ok(mbps_bits) = num_str.parse::<f32>() {
                    live_mb_s = (mbps_bits / 8.0).max(0.0);
                }
            }
        } else if let Some(pos) = line.find(", now ") {
            // "Increasing target number of download connections to 5 (rate was 0.000, now 39.301)"
            let rest = &line[pos + 6..];
            let num_str = rest.trim_end_matches(')').trim();
            if let Ok(mbps_bits) = num_str.parse::<f32>() {
                if mbps_bits > 0.05 {
                    live_mb_s = mbps_bits / 8.0;
                }
            }
        }

        // Track AppID update state transitions:
        if let Some(pos) = line.find("AppID ") {
            let rest = &line[pos + 6..];
            let mut parts = rest.splitn(2, ' ');
            let appid = parts.next().unwrap_or("").trim();
            let detail = parts.next().unwrap_or("");
            if appid.is_empty() || !appid.chars().all(|c| c.is_ascii_digit()) {
                continue;
            }
            if detail.contains("App update changed :") {
                if detail.contains("Running Update") && !detail.contains("Stopping") {
                    if appid != "228980" || active_app.is_none() {
                        active_app = Some(appid.to_string());
                        is_running = true;
                    }
                } else if detail.contains("None") || detail.contains("Stopping") {
                    if active_app.as_deref() == Some(appid) {
                        active_app = None;
                        is_running = false;
                        live_mb_s = 0.0;
                    }
                }
            } else if detail.contains("update canceled")
                || detail.contains("(Suspended)")
                || detail.contains("finished update")
                || detail.contains("finished uninstall")
            {
                if active_app.as_deref() == Some(appid) {
                    active_app = None;
                    is_running = false;
                    live_mb_s = 0.0;
                }
            }
        }
    }
    (active_app, live_mb_s, is_running)
}

/// Scans Steam (Registry + content_log + all steamapps libraries) for an actively running game download.
/// Returns 1 ONLY when a download/patch is genuinely running (never when paused/suspended/finished!).
#[no_mangle]
pub unsafe extern "C" fn rust_poll_steam_download(
    out_name_utf8: *mut u8,
    max_name_len: usize,
    out_pct: *mut f32,
    out_mbps: *mut f32,
) -> i32 {
    if out_name_utf8.is_null() || out_pct.is_null() || out_mbps.is_null() || max_name_len < 4 {
        return 0;
    }
    let (primary_root, dirs) = collect_steam_roots_and_apps();
    let (log_appid, log_mb_s, log_running) = match primary_root {
        Some(ref r) => parse_steam_content_log(r),
        None => (None, 0.0, false),
    };

    // If Steam's own content_log says nothing is running, do NOT show stale/paused ACF flags!
    let mut best: Option<(String, String, u64, u64)> = None;

    for dir in dirs {
        let entries = match fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let fname = entry.file_name();
            let fstr = fname.to_string_lossy();
            if !fstr.starts_with("appmanifest_") || !fstr.ends_with(".acf") {
                continue;
            }
            let content = match fs::read_to_string(entry.path()) {
                Ok(c) => c,
                Err(_) => continue,
            };
            let appid = extract_vdf_field(&content, "appid").unwrap_or("0").to_string();
            let flags: u32 = extract_vdf_field(&content, "StateFlags")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);

            // Bit 2048 (0x800) = UpdatePaused; Bit 256 (0x100) = UpdateRunning
            let is_paused = (flags & 2048) != 0;
            let acf_running = (flags & 256) != 0 && !is_paused;
            let log_matches = log_running && log_appid.as_deref() == Some(appid.as_str());
            if !acf_running && !log_matches {
                continue;
            }
            if !log_running && log_appid.is_none() && !acf_running {
                continue;
            }

            let to_dl: u64 = extract_vdf_field(&content, "BytesToDownload")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let done_dl: u64 = extract_vdf_field(&content, "BytesDownloaded")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let to_st: u64 = extract_vdf_field(&content, "BytesToStage")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let done_st: u64 = extract_vdf_field(&content, "BytesStaged")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);

            let (done_eff, total_eff) = if to_dl > 0 && done_dl < to_dl {
                (done_dl, to_dl)
            } else if to_st > 0 && done_st < to_st {
                (done_st, to_st)
            } else {
                continue;
            };

            let name = extract_vdf_field(&content, "name").unwrap_or("Steam").to_string();
            // Prefer exact log_appid match or non-redist game
            if log_matches {
                best = Some((appid, name, done_eff, total_eff));
                break;
            } else if best.is_none() && appid != "228980" {
                best = Some((appid, name, done_eff, total_eff));
            }
        }
        if best.is_some() && log_running {
            break;
        }
    }

    if let Some((appid, name, done_acf, total_bytes)) = best {
        let mut mb_s = log_mb_s;
        let mut live_done = done_acf as f64;

        if let Ok(mut st) = STEAM_DL_STATE.lock() {
            let now = Instant::now();
            if st.last_app == appid {
                let dt_tick = st.last_tick.map(|t| now.duration_since(t).as_secs_f64()).unwrap_or(0.0).min(3.0);
                if done_acf > st.last_acf_bytes {
                    // ACF just flushed real disk bytes -> compute fallback rate & re-anchor!
                    if let Some(prev_t) = st.last_acf_time {
                        let dt_acf = now.duration_since(prev_t).as_secs_f32();
                        if dt_acf > 0.3 {
                            let acf_rate = ((done_acf - st.last_acf_bytes) as f32) / (1_048_576.0 * dt_acf);
                            if mb_s < 0.05 {
                                mb_s = acf_rate;
                            }
                        }
                    }
                    st.last_acf_bytes = done_acf;
                    st.last_acf_time = Some(now);
                    st.est_bytes = (done_acf as f64).max(st.est_bytes * 0.25 + (done_acf as f64) * 0.75);
                } else if mb_s > 0.05 && dt_tick > 0.0 {
                    // Smoothly integrate live download bytes between delayed Steam ACF flushes!
                    let max_ahead = (done_acf as f64) + (total_bytes as f64) * 0.06;
                    st.est_bytes = (st.est_bytes + (mb_s as f64) * 1_048_576.0 * dt_tick)
                        .min(max_ahead)
                        .min((total_bytes.saturating_sub(1)) as f64);
                }
                if mb_s > 0.01 {
                    st.smooth_mbps = if st.smooth_mbps <= 0.01 {
                        mb_s
                    } else {
                        st.smooth_mbps * 0.6 + mb_s * 0.4
                    };
                }
                st.last_tick = Some(now);
                live_done = st.est_bytes.max(done_acf as f64);
                mb_s = st.smooth_mbps;
            } else {
                st.last_app = appid;
                st.last_acf_bytes = done_acf;
                st.est_bytes = done_acf as f64;
                st.last_acf_time = Some(now);
                st.last_tick = Some(now);
                st.smooth_mbps = mb_s;
            }
        }

        let pct = ((live_done / (total_bytes.max(1) as f64)) * 100.0).clamp(0.0, 99.9) as f32;
        let nb = name.as_bytes();
        let copy_n = nb.len().min(max_name_len - 1);
        let out_slice = std::slice::from_raw_parts_mut(out_name_utf8, max_name_len);
        out_slice[..copy_n].copy_from_slice(&nb[..copy_n]);
        out_slice[copy_n] = 0;
        *out_pct = pct;
        *out_mbps = mb_s;
        return 1;
    }
    0
}

// -----------------------------------------------------------------------------
// Safe Recursive-Descent Math Evaluator in Rust (Feature #2: Spotlight Calc)
// -----------------------------------------------------------------------------
struct ExprParser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> ExprParser<'a> {
    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() && self.bytes[self.pos].is_ascii_whitespace() {
            self.pos += 1;
        }
    }
    fn peek(&mut self) -> Option<u8> {
        self.skip_ws();
        self.bytes.get(self.pos).copied()
    }
    fn parse_expr(&mut self) -> Option<f64> {
        let mut lhs = self.parse_term()?;
        while let Some(c) = self.peek() {
            match c {
                b'+' => {
                    self.pos += 1;
                    lhs += self.parse_term()?;
                }
                b'-' => {
                    self.pos += 1;
                    lhs -= self.parse_term()?;
                }
                _ => break,
            }
        }
        Some(lhs)
    }
    fn parse_term(&mut self) -> Option<f64> {
        let mut lhs = self.parse_power()?;
        while let Some(c) = self.peek() {
            match c {
                b'*' | b'x' | b'X' => {
                    self.pos += 1;
                    lhs *= self.parse_power()?;
                }
                b'/' => {
                    self.pos += 1;
                    let rhs = self.parse_power()?;
                    if rhs.abs() < 1e-15 {
                        return None;
                    }
                    lhs /= rhs;
                }
                b'%' => {
                    self.pos += 1;
                    let rhs = self.parse_power()?;
                    if rhs.abs() < 1e-15 {
                        return None;
                    }
                    lhs %= rhs;
                }
                _ => break,
            }
        }
        Some(lhs)
    }
    fn parse_power(&mut self) -> Option<f64> {
        let base = self.parse_unary()?;
        if self.peek() == Some(b'^') {
            self.pos += 1;
            let exp = self.parse_unary()?;
            return Some(base.powf(exp));
        }
        Some(base)
    }
    fn parse_unary(&mut self) -> Option<f64> {
        match self.peek() {
            Some(b'-') => {
                self.pos += 1;
                Some(-self.parse_unary()?)
            }
            Some(b'+') => {
                self.pos += 1;
                self.parse_unary()
            }
            _ => self.parse_primary(),
        }
    }
    fn parse_primary(&mut self) -> Option<f64> {
        if self.peek() == Some(b'(') {
            self.pos += 1;
            let v = self.parse_expr()?;
            if self.peek() != Some(b')') {
                return None;
            }
            self.pos += 1;
            return Some(v);
        }
        self.skip_ws();
        let start = self.pos;
        let mut has_dot = false;
        while self.pos < self.bytes.len() {
            let b = self.bytes[self.pos];
            if b.is_ascii_digit() {
                self.pos += 1;
            } else if (b == b'.' || b == b',') && !has_dot {
                has_dot = true;
                self.pos += 1;
            } else {
                break;
            }
        }
        if self.pos > start {
            let s = std::str::from_utf8(&self.bytes[start..self.pos]).ok()?.replace(',', ".");
            return s.parse::<f64>().ok();
        }
        None
    }
}

/// Evaluates an arithmetic expression safely in Rust (returns 1 on valid math result, 0 otherwise).
#[no_mangle]
pub unsafe extern "C" fn rust_eval_math_expr(
    expr_utf8: *const u8,
    expr_len: usize,
    out_val: *mut f64,
) -> i32 {
    if expr_utf8.is_null() || out_val.is_null() || expr_len == 0 || expr_len > 256 {
        return 0;
    }
    let slice = std::slice::from_raw_parts(expr_utf8, expr_len);
    // Must contain at least one operator so plain numbers like "2026" don't hijack app search
    let has_op = slice.iter().any(|&b| matches!(b, b'+' | b'-' | b'*' | b'/' | b'^' | b'%' | b'('));
    if !has_op {
        return 0;
    }
    let mut parser = ExprParser { bytes: slice, pos: 0 };
    if let Some(val) = parser.parse_expr() {
        parser.skip_ws();
        if parser.pos == slice.len() && val.is_finite() {
            *out_val = val;
            return 1;
        }
    }
    0
}

/// Native Win32 primary keyboard layout LangID of foreground window (0x19=RU, 0x09=EN, 0x22=UA, 0x07=DE).
#[no_mangle]
pub extern "C" fn rust_get_keyboard_layout() -> u32 {
    unsafe {
        let fg = GetForegroundWindow();
        if fg == 0 {
            return 0x09;
        }
        let tid = GetWindowThreadProcessId(fg, std::ptr::null_mut());
        let klid = GetKeyboardLayout(tid);
        ((klid & 0xFFFF) & 0x3FF) as u32
    }
}

/// Native Win32 pixel color pipette under current cursor position.
#[no_mangle]
pub unsafe extern "C" fn rust_get_cursor_pixel_rgb(
    out_r: *mut u8,
    out_g: *mut u8,
    out_b: *mut u8,
) -> i32 {
    if out_r.is_null() || out_g.is_null() || out_b.is_null() {
        return 0;
    }
    let mut pt = Point::default();
    if GetCursorPos(&mut pt) == 0 {
        return 0;
    }
    let hdc = GetDC(0);
    if hdc == 0 {
        return 0;
    }
    let col = GetPixel(hdc, pt.x, pt.y);
    ReleaseDC(0, hdc);
    if col == 0xFFFF_FFFF {
        return 0;
    }
    *out_r = (col & 0xFF) as u8;
    *out_g = ((col >> 8) & 0xFF) as u8;
    *out_b = ((col >> 16) & 0xFF) as u8;
    1
}

/// Native Win32 battery / AC power status (replaces psutil.sensors_battery).
/// Returns 1 if battery present (pct in 0..=100), 0 if desktop PC (no battery).
#[no_mangle]
pub unsafe extern "C" fn rust_get_power_status(out_pct: *mut i32, out_ac: *mut i32) -> i32 {
    if out_pct.is_null() || out_ac.is_null() {
        return 0;
    }
    let mut st = SystemPowerStatus::default();
    if GetSystemPowerStatus(&mut st) == 0 {
        return 0;
    }
    *out_ac = if st.ac_line_status == 1 { 1 } else { 0 };
    if st.battery_flag == 128 || st.battery_life_percent > 100 {
        *out_pct = -1;
        return 0;
    }
    *out_pct = st.battery_life_percent as i32;
    1
}

/// Reads Windows AppsUseLightTheme from HKCU directly in Rust (1 = Dark, 0 = Light).
#[no_mangle]
pub extern "C" fn rust_get_dark_theme() -> i32 {
    unsafe {
        const HKEY_CURRENT_USER: isize = -2147483647;
        const KEY_READ: u32 = 0x20019;
        let subkey: Vec<u16> = "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize\0"
            .encode_utf16()
            .collect();
        let val_name: Vec<u16> = "AppsUseLightTheme\0".encode_utf16().collect();
        let mut hkey: usize = 0;
        if RegOpenKeyExW(HKEY_CURRENT_USER, subkey.as_ptr(), 0, KEY_READ, &mut hkey) != 0 {
            return 1;
        }
        let mut val: u32 = 0;
        let mut cb: u32 = 4;
        let mut rtype: u32 = 0;
        let ok = RegQueryValueExW(
            hkey,
            val_name.as_ptr(),
            std::ptr::null_mut(),
            &mut rtype,
            (&mut val as *mut u32) as *mut u8,
            &mut cb,
        );
        RegCloseKey(hkey);
        if ok == 0 {
            if val == 0 { 1 } else { 0 }
        } else {
            1
        }
    }
}
