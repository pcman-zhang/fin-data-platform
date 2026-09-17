import { theme as antdTheme } from "antd";
import type { ThemeConfig } from "antd";

export type ThemeMode = "dark" | "light";

const STORAGE_KEY = "fdp.theme";

/** Sider 在两种模式下都保持暗色（导航锚点），仅页面区域随模式切换。 */
export const SIDER_BG = "#171a21";
export const ELEVATED_BG = "#1f232b";
export const BASE_BG = "#0f1115";
export const PRIMARY = "#4c8dff";

export function loadThemeMode(): ThemeMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function saveThemeMode(mode: ThemeMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* 存储不可用时静默降级（仍按内存态渲染） */
  }
}

const baseToken: NonNullable<ThemeConfig["token"]> = {
  colorPrimary: PRIMARY,
  fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
  fontFamilyCode: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: 14,
  fontSizeSM: 12,
  fontSizeLG: 16,
  borderRadius: 6,
};

const darkToken: NonNullable<ThemeConfig["token"]> = {
  ...baseToken,
  colorBgLayout: BASE_BG,
  colorBgContainer: SIDER_BG,
  colorBgElevated: ELEVATED_BG,
  colorText: "#e5e7eb",
  colorTextSecondary: "#9ba3af",
  colorTextTertiary: "#7a8391",
  colorTextDescription: "#9ba3af",
  colorTextDisabled: "#5b6472",
  colorSplit: "#2a2f3a",
  colorBorder: "#2a2f3a",
  colorBorderSecondary: "#2a2f3a",
  colorSuccess: "#3fb950",
  colorInfo: PRIMARY,
  colorWarning: "#d29922",
  colorError: "#f85149",
};

export function themeConfig(mode: ThemeMode): ThemeConfig {
  const dark = mode === "dark";
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: dark ? darkToken : baseToken,
    components: {
      Layout: {
        siderBg: SIDER_BG,
        headerBg: dark ? SIDER_BG : "#ffffff",
        bodyBg: dark ? BASE_BG : "#f5f6f8",
        triggerBg: dark ? ELEVATED_BG : "#e9ebef",
        headerHeight: 64,
        headerPadding: "0 16px",
      },
      Menu: {
        darkItemBg: SIDER_BG,
        darkSubMenuItemBg: BASE_BG,
        darkItemColor: "#9ba3af",
        darkItemHoverBg: ELEVATED_BG,
        darkItemSelectedBg: PRIMARY,
        darkItemSelectedColor: "#ffffff",
      },
    },
  };
}
