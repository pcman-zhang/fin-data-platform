import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "antd/dist/reset.css";
import { StrictMode, useCallback, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";
import { loadThemeMode, saveThemeMode, themeConfig, type ThemeMode } from "./theme";

dayjs.locale("zh-cn");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 15_000, refetchOnWindowFocus: false },
  },
});

function Root() {
  const [mode, setMode] = useState<ThemeMode>(loadThemeMode);
  const toggleMode = useCallback(() => {
    setMode((prev) => {
      const next: ThemeMode = prev === "dark" ? "light" : "dark";
      saveThemeMode(next);
      return next;
    });
  }, []);

  return (
    <ConfigProvider theme={themeConfig(mode)} locale={zhCN} componentSize="medium" tooltip={{ unique: true }}>
      <AntdApp message={{ maxCount: 3 }}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App mode={mode} onToggleMode={toggleMode} />
          </BrowserRouter>
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
