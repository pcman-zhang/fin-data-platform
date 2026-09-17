import { CopyOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, App as AntdApp, Badge, Button, Divider, Drawer, Flex, Typography, theme } from "antd";
import type { TablePaginationConfig } from "antd";
import type { ReactNode } from "react";
import { useRef, useState } from "react";

import { formatDateTime } from "./api";

/** 任务状态 → §2 语义色（全局唯一映射）。 */
const RUN_STATUS: Record<string, { status: "success" | "processing" | "warning" | "error" | "default"; text: string }> = {
  succeeded: { status: "success", text: "succeeded" },
  running: { status: "processing", text: "running" },
  retrying: { status: "processing", text: "retrying" },
  queued: { status: "warning", text: "queued" },
  failed: { status: "error", text: "failed" },
  dead: { status: "error", text: "dead" },
  cancelled: { status: "default", text: "cancelled" },
  interrupted: { status: "default", text: "interrupted" },
};

export function StatusBadge({ status, size = "small" }: { status: string; size?: "small" | "default" }) {
  const config = RUN_STATUS[status] ?? { status: "default" as const, text: status };
  return <Badge status={config.status} text={config.text} size={size} />;
}

/** 实体生命周期（存续 / 已关闭）→ §2 语义色。 */
const LIVE_STATUS = new Set(["listed", "active", "存续", "正常"]);

export function EntityStatusBadge({ socialStatus, validTo }: { socialStatus: string | null; validTo: string | null }) {
  const text = socialStatus ?? (validTo ? "已关闭" : "存续");
  const status = validTo ? "default" : LIVE_STATUS.has(text) ? "success" : "default";
  return <Badge status={status} text={text} size="small" />;
}

export function Mono({ children, secondary = false }: { children: ReactNode; secondary?: boolean }) {
  const { token } = theme.useToken();
  return (
    <span style={{ fontFamily: token.fontFamilyCode, color: secondary ? token.colorTextSecondary : undefined }}>
      {children}
    </span>
  );
}

export function Num({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  const { token } = theme.useToken();
  return (
    <span style={{ fontVariantNumeric: "tabular-nums", color: muted ? token.colorTextSecondary : undefined }}>
      {children}
    </span>
  );
}

export function TimeText({ value }: { value: string | null | undefined }) {
  const { token } = theme.useToken();
  const empty = formatDateTime(value) === "—";
  return (
    <span style={{ fontFamily: token.fontFamilyCode, fontVariantNumeric: "tabular-nums", color: empty ? token.colorTextTertiary : token.colorTextSecondary }}>
      {formatDateTime(value)}
    </span>
  );
}

/** 区块分段标题（§9.1：solid + token 色 + 正文样式）。 */
export function SectionDivider({ title }: { title: ReactNode }) {
  return (
    <Divider titlePlacement="start" plain size="small" style={{ marginBlock: 16 }}>
      {title}
    </Divider>
  );
}

/** 错误全文（§4：Drawer 内 <pre> + 复制按钮）。 */
export function ErrorText({ text }: { text: string }) {
  const { token } = theme.useToken();
  const { message } = AntdApp.useApp();
  const copy = () => {
    void navigator.clipboard.writeText(text).then(
      () => message.success("错误详情已复制"),
      () => message.error("复制失败：浏览器未授予剪贴板权限"),
    );
  };
  return (
    <Flex vertical gap={12} align="flex-start">
      <pre
        style={{
          margin: 0,
          padding: 12,
          width: "100%",
          maxHeight: 400,
          overflow: "auto",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          fontFamily: token.fontFamilyCode,
          fontSize: 12,
          lineHeight: 1.6,
          color: token.colorError,
          background: token.colorFillQuaternary,
          border: `1px solid ${token.colorSplit}`,
          borderRadius: token.borderRadius,
        }}
      >
        {text}
      </pre>
      <Button icon={<CopyOutlined />} onClick={copy}>
        复制
      </Button>
    </Flex>
  );
}

/** 接口错误内联提示（§9.6）：摘要可见，全文进 Drawer。 */
export function ErrorAlert({ error, title = "请求失败" }: { error: unknown; title?: string }) {
  const [open, setOpen] = useState(false);
  const detail = error instanceof Error ? error.message : String(error);
  const truncated = detail.length > 160;
  return (
    <>
      <Alert
        type="error"
        showIcon
        variant="filled"
        title={title}
        description={<Mono>{truncated ? `${detail.slice(0, 160)}…` : detail}</Mono>}
        action={
          truncated ? (
            <Button size="small" onClick={() => setOpen(true)}>
              查看详情
            </Button>
          ) : undefined
        }
      />
      <Drawer open={open} onClose={() => setOpen(false)} title="错误详情" size={560} destroyOnHidden>
        <ErrorText text={detail} />
      </Drawer>
    </>
  );
}

/** 渲染兜底（§6）：展示错误信息 + 刷新入口，不让白屏。 */
export function RouteBoundary({ children }: { children: ReactNode }) {
  return (
    <Alert.ErrorBoundary
      description={
        <Flex vertical gap={8} align="flex-start">
          <Typography.Text type="secondary">页面渲染异常，请刷新重试；若持续出现请反馈。</Typography.Text>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => window.location.reload()}>
            刷新页面
          </Button>
        </Flex>
      }
    >
      {children}
    </Alert.ErrorBoundary>
  );
}

/** 表格分页参数（§1：20/50/100，默认 50，本地记忆）。 */
const PAGE_SIZES = [20, 50, 100];

export function usePageSize(storageKey: string, fallback = 50): [number, (size: number) => void] {
  const [pageSize, setPageSize] = useState<number>(() => {
    const raw = Number(localStorage.getItem(storageKey));
    return PAGE_SIZES.includes(raw) ? raw : fallback;
  });
  const change = (size: number) => {
    try {
      localStorage.setItem(storageKey, String(size));
    } catch {
      /* 忽略存储失败 */
    }
    setPageSize(size);
  };
  return [pageSize, change];
}

export function paginationConfig(
  pageSize: number,
  onPageSizeChange: (size: number) => void,
): TablePaginationConfig {
  return {
    pageSize,
    showSizeChanger: true,
    pageSizeOptions: PAGE_SIZES.map(String),
    showTotal: (total: number, range: [number, number]) => `${range[0]}–${range[1]} / ${total}`,
    placement: ["bottomEnd"],
    onChange: (_page: number, size: number) => {
      if (size !== pageSize) onPageSizeChange(size);
    },
  };
}

/** Drawer 宽度（§9.9：默认 560 / maxSize 840，localStorage 记忆）。 */
export function useDrawerWidth(storageKey = "fdp.drawer.width", fallback = 560) {
  const [width, setWidth] = useState<number>(() => {
    try {
      const raw = Number(localStorage.getItem(storageKey));
      return raw >= 480 && raw <= 840 ? raw : fallback;
    } catch {
      return fallback;
    }
  });
  const latest = useRef(width);
  const resizable = {
    onResize: (size: number) => {
      latest.current = size;
      setWidth(size);
    },
    onResizeEnd: () => {
      try {
        localStorage.setItem(storageKey, String(Math.round(latest.current)));
      } catch {
        /* 忽略存储失败 */
      }
    },
  };
  return { width, resizable } as const;
}

/** 行点击 = 打开详情 Drawer，支持 Enter（§7）。 */
export function rowActivate<T>(onActivate: (row: T) => void) {
  return (row: T) => ({
    onClick: () => onActivate(row),
    onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => {
      if (event.key === "Enter") onActivate(row);
    },
    tabIndex: 0,
    style: { cursor: "pointer" },
  });
}
