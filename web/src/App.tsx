import {
  ApartmentOutlined,
  DashboardOutlined,
  FunctionOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoonOutlined,
  ScheduleOutlined,
  SunOutlined,
  TableOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Divider, Layout, Menu, Result, Space, Tooltip, Typography, theme } from "antd";
import type { MenuProps } from "antd";
import { useState } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { api } from "./api";
import Algorithms from "./pages/Algorithms";
import Datasets from "./pages/Datasets";
import Entities from "./pages/Entities";
import Jobs from "./pages/Jobs";
import Overview from "./pages/Overview";
import type { ThemeMode } from "./theme";
import { RouteBoundary } from "./ui";

const { Sider, Header, Content } = Layout;

const NAV: MenuProps["items"] = [
  { key: "/", label: "总览", icon: <DashboardOutlined /> },
  { key: "/datasets", label: "数据集", icon: <TableOutlined /> },
  { key: "/entities", label: "实体注册表", icon: <ApartmentOutlined /> },
  { key: "/jobs", label: "任务", icon: <ScheduleOutlined /> },
  { key: "/algorithms", label: "算法", icon: <FunctionOutlined /> },
];

const TITLES: Record<string, string> = {
  "/": "总览",
  "/datasets": "数据集",
  "/entities": "实体注册表",
  "/jobs": "任务与水位",
  "/algorithms": "派生与算法",
};

function HealthBadge() {
  const { data, isError, isLoading } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 30_000,
  });
  if (isLoading) return <Badge status="processing" text="检查中…" />;
  if (isError) return <Badge status="error" text="服务不可达" />;
  const ok = data?.ok === true;
  return <Badge status={ok ? "success" : "warning"} text={ok ? "服务正常" : "检查未通过"} />;
}

function useCollapsed() {
  const [value, setValue] = useState<boolean>(() => {
    try {
      return localStorage.getItem("fdp.sider.collapsed") === "1";
    } catch {
      return false;
    }
  });
  const set = (next: boolean) => {
    setValue(next);
    try {
      localStorage.setItem("fdp.sider.collapsed", next ? "1" : "0");
    } catch {
      /* 忽略存储失败 */
    }
  };
  return [value, set] as const;
}

export default function App({ mode, onToggleMode }: { mode: ThemeMode; onToggleMode: () => void }) {
  const [collapsed, setCollapsed] = useCollapsed();
  const location = useLocation();
  const navigate = useNavigate();
  const { token } = theme.useToken();
  const known = NAV?.some((item) => item && "key" in item && item.key === location.pathname) ?? false;
  const selected = known ? location.pathname : "/";

  return (
    <Layout hasSider>
      <Sider
        theme="dark"
        width={200}
        collapsedWidth={80}
        collapsible
        collapsed={collapsed}
        trigger={null}
        breakpoint="lg"
        onBreakpoint={(broken) => setCollapsed(broken)}
        style={{ position: "fixed", insetInlineStart: 0, top: 0, bottom: 0, height: "100vh", overflow: "auto" }}
      >
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selected]}
          items={NAV}
          onClick={({ key }) => navigate(key)}
          style={{ paddingTop: 12, borderInlineEnd: 0 }}
        />
      </Sider>
      <Layout style={{ marginInlineStart: collapsed ? 80 : 200, transition: "margin-inline-start 0.2s" }}>
        <Header
          style={{
            position: "sticky",
            top: 0,
            zIndex: 20,
            display: "flex",
            alignItems: "center",
            gap: 12,
            borderBottom: `1px solid ${token.colorSplit}`,
          }}
        >
          <Button
            type="text"
            aria-label={collapsed ? "展开导航" : "收起导航"}
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed(!collapsed)}
          />
          <Typography.Text strong style={{ fontSize: 16, whiteSpace: "nowrap" }}>
            FinDataPlatform 控制台
          </Typography.Text>
          <Divider vertical style={{ margin: 0 }} />
          <Typography.Text type="secondary" style={{ whiteSpace: "nowrap" }}>
            {TITLES[selected] ?? "总览"}
          </Typography.Text>
          <Space size={8} style={{ marginInlineStart: "auto" }} align="center">
            <HealthBadge />
            <Tooltip title={mode === "dark" ? "切换到亮色" : "切换到暗色"} placement="bottom">
              <Button
                type="text"
                aria-label="切换主题"
                icon={mode === "dark" ? <MoonOutlined /> : <SunOutlined />}
                onClick={onToggleMode}
              />
            </Tooltip>
          </Space>
        </Header>
        <Content style={{ padding: "16px 24px" }}>
          <Routes>
            <Route
              path="/"
              element={
                <RouteBoundary>
                  <Overview />
                </RouteBoundary>
              }
            />
            <Route
              path="/datasets"
              element={
                <RouteBoundary>
                  <Datasets />
                </RouteBoundary>
              }
            />
            <Route
              path="/entities"
              element={
                <RouteBoundary>
                  <Entities />
                </RouteBoundary>
              }
            />
            <Route
              path="/jobs"
              element={
                <RouteBoundary>
                  <Jobs />
                </RouteBoundary>
              }
            />
            <Route
              path="/algorithms"
              element={
                <RouteBoundary>
                  <Algorithms />
                </RouteBoundary>
              }
            />
            <Route
              path="*"
              element={
                <Result
                  status="404"
                  title="404"
                  subTitle="页面不存在"
                  extra={
                    <Button type="primary" onClick={() => navigate("/")}>
                      返回总览
                    </Button>
                  }
                />
              }
            />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}
