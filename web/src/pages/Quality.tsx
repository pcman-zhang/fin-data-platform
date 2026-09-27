import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useState } from "react";

import { api } from "../api";
import type { QualityResult, QualitySummaryItem } from "../api";
import { ErrorAlert, Mono, TimeText, rowActivate, useDrawerWidth } from "../ui";

const STATUS_META: Record<string, { color: string; label: string }> = {
  passed: { color: "green", label: "通过" },
  failed: { color: "red", label: "失败" },
  skipped: { color: "default", label: "跳过" },
  error: { color: "volcano", label: "异常" },
};

function StatusTag({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? { color: "default", label: status };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

function conclusion(item: QualitySummaryItem) {
  const bad = item.failed + item.error;
  if (bad > 0) return <Tag color="red">失败 {bad}</Tag>;
  if (item.passed > 0) return <Tag color="green">通过</Tag>;
  return <Tag>无检查</Tag>;
}

function formatRatio(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`;
}

export default function Quality() {
  const [day, setDay] = useState<string | null>(null);
  const [selected, setSelected] = useState<QualitySummaryItem | null>(null);
  const { width, resizable } = useDrawerWidth();

  const summary = useQuery({
    queryKey: ["quality-summary", day],
    queryFn: () => api.qualitySummary(day ?? undefined),
  });
  const effectiveDay = day ?? summary.data?.day ?? undefined;

  const detail = useQuery({
    queryKey: ["quality-results", effectiveDay, selected?.dataset],
    queryFn: () =>
      api.qualityResults({ date: effectiveDay, dataset: selected?.dataset, limit: 200 }),
    enabled: selected !== null,
  });

  const columns: ColumnsType<QualitySummaryItem> = [
    {
      title: "数据集",
      dataIndex: "dataset",
      width: 230,
      fixed: "start",
      render: (value: string) => <Mono>{value}</Mono>,
    },
    {
      title: "结论",
      key: "conclusion",
      width: 100,
      render: (_, row) => conclusion(row),
    },
    { title: "检查", dataIndex: "total", width: 72, align: "right" },
    {
      title: "失败",
      dataIndex: "failed",
      width: 72,
      align: "right",
      render: (value: number, row) => (
        <Tooltip
          title={row.failed_checks.length > 0 ? row.failed_checks.join("、") : undefined}
          placement="topLeft"
        >
          <span>{value + row.error}</span>
        </Tooltip>
      ),
    },
    { title: "告警", dataIndex: "warnings", width: 72, align: "right" },
    { title: "违规", dataIndex: "violations", width: 84, align: "right" },
    {
      title: "覆盖率",
      dataIndex: "coverage_ratio",
      width: 100,
      align: "right",
      render: (value: number | null) => formatRatio(value),
    },
    {
      title: "时效滞后",
      dataIndex: "freshness_lag_days",
      width: 100,
      align: "right",
      render: (value: number | null) =>
        value === null ? "—" : `${value} 个交易日`,
    },
  ];

  const detailColumns: ColumnsType<QualityResult> = [
    {
      title: "检查项",
      dataIndex: "check_id",
      width: 230,
      render: (value: string) => <Mono>{value}</Mono>,
    },
    {
      title: "家族",
      dataIndex: "family",
      width: 112,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: "严重级",
      dataIndex: "severity",
      width: 88,
      render: (value: string) => (
        <Tag color={value === "error" ? "red" : "gold"}>{value}</Tag>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 88,
      render: (value: string) => <StatusTag status={value} />,
    },
    { title: "违规", dataIndex: "violations", width: 72, align: "right" },
    {
      title: "消息",
      dataIndex: "message",
      ellipsis: { showTitle: false },
      render: (value: string) => (
        <Tooltip title={value} placement="topLeft">
          <span>{value}</span>
        </Tooltip>
      ),
    },
  ];

  return (
    <>
      <Card
        variant="borderless"
        title="数据质量"
        extra={
          <Space size={8} wrap>
            <Space size={6}>
              <Typography.Text type="secondary">报告日</Typography.Text>
              <DatePicker
                allowClear
                value={
                  day
                    ? dayjs(day)
                    : summary.data?.day
                      ? dayjs(summary.data.day)
                      : undefined
                }
                onChange={(value) => {
                  setSelected(null);
                  setDay(value ? value.format("YYYY-MM-DD") : null);
                }}
                disabledDate={(current) => current.isAfter(dayjs(), "day")}
              />
            </Space>
            <Typography.Text type="secondary">
              生成于 <TimeText value={summary.data?.generated_at} />
            </Typography.Text>
            <Tooltip title="刷新" placement="top">
              <Button
                type="text"
                aria-label="刷新质量报告"
                icon={<ReloadOutlined />}
                onClick={() => void summary.refetch()}
              />
            </Tooltip>
          </Space>
        }
        styles={{ body: { paddingTop: 16 } }}
      >
        {summary.isError ? <ErrorAlert error={summary.error} title="质量报告加载失败" /> : null}
        <Table<QualitySummaryItem>
          rowKey="dataset"
          dataSource={summary.data?.datasets ?? []}
          loading={summary.isLoading}
          size="medium"
          scroll={{ x: "max-content" }}
          onRow={rowActivate((row) => setSelected(row))}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无质量报告（在「任务」页触发全局任务 quality.scan）"
              />
            ),
          }}
          pagination={false}
          columns={columns}
        />
      </Card>
      <Drawer
        title="质量明细"
        size={width}
        resizable={resizable}
        open={selected !== null}
        onClose={() => setSelected(null)}
        destroyOnHidden
        loading={detail.isLoading}
      >
        {detail.isError ? <ErrorAlert error={detail.error} title="质量明细加载失败" /> : null}
        <Flex vertical gap={12}>
          <Descriptions
            size="small"
            column={1}
            items={[
              {
                key: "dataset",
                label: "数据集",
                children: <Mono>{selected?.dataset ?? "—"}</Mono>,
              },
              {
                key: "day",
                label: "报告日",
                children: <Mono>{effectiveDay ?? "—"}</Mono>,
              },
              {
                key: "total",
                label: "检查项",
                children: selected
                  ? `${selected.total}（失败 ${selected.failed + selected.error} · 告警 ${selected.warnings}）`
                  : "—",
              },
            ]}
          />
          <Table<QualityResult>
            rowKey={(row) => `${row.dataset}:${row.check_id}`}
            dataSource={detail.data?.items ?? []}
            size="small"
            pagination={false}
            scroll={{ x: "max-content" }}
            columns={detailColumns}
            expandable={{
              expandedRowRender: (row) => (
                <Flex vertical gap={8}>
                  {row.samples.length > 0 ? (
                    <div>
                      <Typography.Text type="secondary">
                        违规样本（{row.samples.length}）
                      </Typography.Text>
                      <ul style={{ margin: "4px 0 0", paddingInlineStart: 20 }}>
                        {row.samples.map((sample) => (
                          <li key={sample}>
                            <Typography.Text code>{sample}</Typography.Text>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                  <div>
                    <Typography.Text type="secondary">指标</Typography.Text>
                    <pre style={{ margin: "4px 0 0", fontSize: 12 }}>
                      {JSON.stringify(row.metrics, null, 2)}
                    </pre>
                  </div>
                </Flex>
              ),
            }}
          />
        </Flex>
      </Drawer>
    </>
  );
}
