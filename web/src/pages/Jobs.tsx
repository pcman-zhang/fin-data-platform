import { PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntdApp,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tooltip,
  Typography,
  theme,
} from "antd";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, formatDuration, JOB_STATUSES } from "../api";
import type { JobRun, SyncItem, SyncRequest, SyncResponse, Watermark } from "../api";
import {
  ErrorAlert,
  ErrorText,
  Mono,
  Num,
  SectionDivider,
  StatusBadge,
  TimeText,
  paginationConfig,
  rowActivate,
  useDrawerWidth,
  usePageSize,
} from "../ui";

const DATASET = "cn_equity.daily_bar";
const CODE_PATTERN = /^\d{6}\.(SH|SZ|BJ|OF)$/;

interface SyncFormValues {
  codes: string;
  window?: [Dayjs, Dayjs] | null;
  request_id?: string;
}

function parseCodes(text: string): string[] {
  return [...new Set(text.split(/[\s,，;；]+/).map((code) => code.trim()).filter(Boolean))];
}

const WINDOW_PRESETS: { label: string; value: () => [Dayjs, Dayjs] }[] = [
  { label: "近 7 日", value: () => [dayjs().subtract(6, "day"), dayjs()] },
  { label: "近 20 日", value: () => [dayjs().subtract(19, "day"), dayjs()] },
  { label: "本月至今", value: () => [dayjs().startOf("month"), dayjs()] },
];

function SyncForm({ onDone }: { onDone: (result: SyncResponse) => void }) {
  const { token } = theme.useToken();
  const { message, modal } = AntdApp.useApp();
  const [form] = Form.useForm<SyncFormValues>();
  const codesText = Form.useWatch("codes", form) ?? "";
  const codeList = parseCodes(codesText);

  const mutation = useMutation({
    mutationFn: (payload: SyncRequest) => api.sync(payload),
    onSuccess: (result) => {
      message.success(`已提交 ${result.submitted.length} 条同步任务`);
      onDone(result);
    },
  });

  const submit = (values: SyncFormValues) => {
    const codes = parseCodes(values.codes);
    const window = values.window ?? null;
    const requestId = values.request_id?.trim() || `web-${Date.now()}`;
    const windowText = window ? `${window[0].format("YYYY-MM-DD")} ~ ${window[1].format("YYYY-MM-DD")}` : "水位+1 ~ 今日";
    modal.confirm({
      title: "确认触发同步",
      centered: true,
      width: 480,
      okText: "确认提交",
      cancelText: "取消",
      closable: false,
      mask: { enabled: true, closable: false },
      focusable: { autoFocusButton: "cancel" },
      content: (
        <Flex vertical gap={8}>
          <Descriptions
            column={1}
            size="small"
            items={[
              { key: "dataset", label: "数据集", children: <Mono>{DATASET}</Mono> },
              { key: "codes", label: "代码", children: <Mono>{codes.join(", ")}</Mono> },
              { key: "window", label: "窗口", children: <Mono>{windowText}</Mono> },
              { key: "request", label: "幂等键", children: <Mono>{requestId}</Mono> },
            ]}
          />
          <Typography.Text type="secondary">窗口不得晚于今天；重复提交由幂等键与窗口去重。</Typography.Text>
        </Flex>
      ),
      onOk: () =>
        mutation.mutateAsync({
          codes,
          dataset: DATASET,
          start: window ? window[0].format("YYYY-MM-DD") : null,
          end: window ? window[1].format("YYYY-MM-DD") : null,
          request_id: requestId,
        }),
    });
  };

  return (
    <Form
      form={form}
      layout="vertical"
      requiredMark
      initialValues={{ codes: "600519.SH" }}
      onFinish={submit}
      onFinishFailed={({ errorFields }) => {
        void form.scrollToField(errorFields[0]?.name ?? "codes");
      }}
    >
      <Form.Item
        name="codes"
        label="代码"
        validateFirst
        rules={[
          { required: true, message: "代码：请填写至少一个 canonical 代码，如 600519.SH" },
          {
            validator: (_rule, value: string) => {
              const codes = parseCodes(value ?? "");
              const invalid = codes.filter((code) => !CODE_PATTERN.test(code));
              if (invalid.length > 0) {
                return Promise.reject(
                  new Error(`代码：${invalid.slice(0, 3).join("、")} 格式不正确，应为 600519.SH 形式`),
                );
              }
              if (codes.length > 200) {
                return Promise.reject(new Error(`代码：最多 200 个，当前 ${codes.length} 个，请拆分提交`));
              }
              return Promise.resolve();
            },
          },
        ]}
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {`已解析 ${codeList.length} 个代码`}
          </Typography.Text>
        }
      >
        <Input.TextArea
          autoSize={{ minRows: 2, maxRows: 6 }}
          allowClear
          placeholder="600519.SH, 000001.SZ"
          style={{ fontFamily: token.fontFamilyCode, fontSize: 12 }}
        />
      </Form.Item>

      <Form.Item
        name="window"
        label="窗口（可选）"
        rules={[
          {
            validator: (_rule, value?: [Dayjs, Dayjs] | null) => {
              if (!value || !value[0] || !value[1]) return Promise.resolve();
              if (value[0].isAfter(value[1], "day")) {
                return Promise.reject(new Error("窗口：起点不得晚于终点，请重新选择"));
              }
              if (value[1].isAfter(dayjs(), "day")) {
                return Promise.reject(new Error("窗口：终点不得晚于今天，请调整窗口"));
              }
              return Promise.resolve();
            },
          },
        ]}
        extra={
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            留空 = 起点取水位 +1、终点取今日
          </Typography.Text>
        }
      >
        <DatePicker.RangePicker
          style={{ width: "100%" }}
          format="YYYY-MM-DD"
          allowClear
          disabledDate={(current) => current.isAfter(dayjs().endOf("day"))}
          presets={WINDOW_PRESETS}
          placeholder={["水位+1", "今日"]}
        />
      </Form.Item>

      <Form.Item
        name="request_id"
        label="request_id（幂等键，可选）"
        rules={[{ max: 64, message: "request_id：最多 64 个字符，请缩短后重试" }]}
      >
        <Input placeholder="留空自动生成 web-<时间戳>" />
      </Form.Item>

      {mutation.isError ? <ErrorAlert error={mutation.error} title="同步提交失败" /> : null}

      <Flex vertical gap={8} style={{ marginTop: 16 }}>
        <Button
          block
          type="primary"
          htmlType="submit"
          icon={<PlayCircleOutlined />}
          loading={mutation.isPending}
          disabled={codeList.length === 0}
        >
          {`触发同步（${codeList.length} 个代码）`}
        </Button>
        <Button block type="text" htmlType="button" onClick={() => form.resetFields()}>
          重置
        </Button>
      </Flex>
    </Form>
  );
}

function ResultAlerts({ result }: { result: SyncResponse }) {
  return (
    <Flex vertical gap={8} style={{ marginTop: 16 }}>
      {result.submitted.length > 0 ? (
        <Alert
          type="success"
          showIcon
          variant="filled"
          title={`已提交 ${result.submitted.length} 条`}
          description={
            <Flex vertical gap={4}>
              {result.submitted.map((item) => (
                <SyncItemLine key={item.code} item={item} />
              ))}
            </Flex>
          }
        />
      ) : null}
      {result.skipped.length > 0 ? (
        <Alert
          type="warning"
          showIcon
          variant="filled"
          closable
          title={`已跳过 ${result.skipped.length} 条`}
          description={
            <Flex vertical gap={4}>
              {result.skipped.map((item) => (
                <SyncItemLine key={item.code} item={item} />
              ))}
            </Flex>
          }
        />
      ) : null}
    </Flex>
  );
}

function SyncItemLine({ item }: { item: SyncItem }) {
  const window = item.window_start ? `${item.window_start} ~ ${item.window_end}` : "—";
  const detail = [item.run_id !== null ? `run ${item.run_id}` : item.status, window, item.note ?? ""]
    .filter(Boolean)
    .join(" · ");
  return (
    <Flex align="center" gap={8} wrap>
      <Mono>{item.code}</Mono>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {detail}
      </Typography.Text>
    </Flex>
  );
}

export default function Jobs() {
  const [params, setParams] = useSearchParams();
  const selectedParam = params.get("run");
  const selected = selectedParam ? Number(selectedParam) : null;

  const [status, setStatus] = useState("");
  const [jobId, setJobId] = useState("");
  const [result, setResult] = useState<SyncResponse | null>(null);
  const [runPageSize, setRunPageSize] = usePageSize("fdp.pageSize.jobs");
  const [markPageSize, setMarkPageSize] = usePageSize("fdp.pageSize.watermarks", 20);
  const queryClient = useQueryClient();

  const jobs = useQuery({
    queryKey: ["jobs", status, jobId],
    queryFn: () => api.jobs({ status: status || undefined, job_id: jobId || undefined, limit: 200 }),
  });
  const watermarks = useQuery({ queryKey: ["watermarks"], queryFn: api.watermarks });

  const activate = () => {
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    void queryClient.invalidateQueries({ queryKey: ["watermarks"] });
  };

  const open = (runId: number) => {
    const next = new URLSearchParams(params);
    next.set("run", String(runId));
    setParams(next);
  };
  const close = () => {
    const next = new URLSearchParams(params);
    next.delete("run");
    setParams(next, { replace: true });
  };

  return (
    <Flex gap={16} align="flex-start">
      <Flex vertical gap={16} style={{ flex: 1, minWidth: 0 }}>
        <Card
          variant="borderless"
          title="任务运行"
          extra={
            <Space size={8} wrap>
              <Select
                allowClear
                options={JOB_STATUSES.map((value) => ({ value, label: value }))}
                value={status || undefined}
                onChange={(value) => setStatus(value ?? "")}
                placeholder="全部状态"
                style={{ width: 148 }}
              />
              <Input.Search
                allowClear
                placeholder="job_id 过滤"
                onSearch={setJobId}
                onChange={(event) => {
                  if (event.target.value === "") setJobId("");
                }}
                style={{ width: 220 }}
              />
              <Tooltip title="刷新" placement="top">
                <Button type="text" aria-label="刷新" icon={<ReloadOutlined />} onClick={activate} />
              </Tooltip>
            </Space>
          }
        >
          {jobs.isError ? <ErrorAlert error={jobs.error} title="任务列表加载失败" /> : null}
          <Table<JobRun>
            rowKey="run_id"
            dataSource={jobs.data ?? []}
            loading={jobs.isLoading}
            size="medium"
            scroll={{ x: "max-content", y: "calc(100vh - 360px)" }}
            onRow={rowActivate((row) => open(row.run_id))}
            rowClassName={(row) => (row.run_id === selected ? "ant-table-row-selected" : "")}
            locale={{
              emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务运行记录" />,
            }}
            pagination={paginationConfig(runPageSize, setRunPageSize)}
            columns={[
              {
                title: "Run",
                dataIndex: "run_id",
                width: 88,
                fixed: "start",
                render: (value: number) => <Typography.Text code>{value}</Typography.Text>,
              },
              {
                title: "代码",
                dataIndex: "scope",
                width: 120,
                render: (value: string) => <Mono>{value}</Mono>,
              },
              {
                title: "数据集",
                dataIndex: "dataset",
                ellipsis: { showTitle: false },
                render: (value: string) => (
                  <Tooltip title={value} placement="topLeft">
                    <Mono secondary>{value}</Mono>
                  </Tooltip>
                ),
              },
              {
                title: "状态",
                dataIndex: "status",
                width: 96,
                render: (value: string) => <StatusBadge status={value} />,
              },
              {
                title: "窗口",
                dataIndex: "window_start",
                width: 180,
                render: (_value: string | null, row) => (
                  <Mono secondary>{row.window_start ? `${row.window_start} ~ ${row.window_end}` : "—"}</Mono>
                ),
              },
              {
                title: "尝试",
                dataIndex: "attempt",
                width: 88,
                align: "right",
                render: (_value: number, row) => <Num muted>{`${row.attempt}/${row.max_attempts}`}</Num>,
              },
              {
                title: "耗时",
                key: "duration",
                width: 88,
                align: "right",
                render: (_value: unknown, row) => (
                  <Mono secondary>{formatDuration(row.started_at, row.finished_at)}</Mono>
                ),
              },
              {
                title: "行数",
                dataIndex: "rows_written",
                width: 88,
                align: "right",
                render: (value: number | null) =>
                  value === null ? <Mono secondary>—</Mono> : <Num>{value.toLocaleString("zh-CN")}</Num>,
              },
              {
                title: "错误",
                dataIndex: "error",
                ellipsis: { showTitle: false },
                render: (value: string | null) =>
                  value ? (
                    <Tooltip title={value} placement="topLeft">
                      <Typography.Text type="danger" style={{ maxWidth: 320 }}>
                        {value}
                      </Typography.Text>
                    </Tooltip>
                  ) : (
                    <Mono secondary>—</Mono>
                  ),
              },
            ]}
          />
        </Card>

        <Card variant="borderless" title="数据水位">
          <Table<Watermark>
            rowKey={(mark) => `${mark.dataset}:${mark.scope}`}
            dataSource={watermarks.data ?? []}
            loading={watermarks.isLoading}
            size="medium"
            pagination={{ ...paginationConfig(markPageSize, setMarkPageSize), hideOnSinglePage: true }}
            locale={{
              emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无水位记录" />,
            }}
            columns={[
              { title: "数据集", dataIndex: "dataset", width: 260, render: (value: string) => <Mono>{value}</Mono> },
              {
                title: "范围",
                dataIndex: "scope",
                width: 140,
                render: (value: string) => <Mono secondary>{value || "—"}</Mono>,
              },
              {
                title: "水位",
                dataIndex: "watermark_time",
                render: (value: string | null) => <TimeText value={value} />,
              },
            ]}
          />
        </Card>
      </Flex>

      <div style={{ width: 380, flexShrink: 0 }}>
        <Card
          variant="borderless"
          title="触发同步"
          extra={<Typography.Text type="secondary">高危操作二次确认</Typography.Text>}
        >
          <SyncForm
            onDone={(next) => {
              setResult(next);
              activate();
            }}
          />
          {result ? <ResultAlerts result={result} /> : null}
        </Card>
      </div>

      <RunDrawer runId={selected} onClose={close} />
    </Flex>
  );
}

function RunDrawer({ runId, onClose }: { runId: number | null; onClose: () => void }) {
  const { width, resizable } = useDrawerWidth();
  const detail = useQuery({
    queryKey: ["job", runId],
    queryFn: () => api.job(runId!),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "running" || status === "retrying" || status === "queued" ? 5000 : false;
    },
  });
  const data = detail.data;

  return (
    <Drawer
      open={runId !== null}
      onClose={onClose}
      title={data ? `运行 #${data.run_id}` : "任务详情"}
      size={width}
      resizable={resizable}
      destroyOnHidden
      loading={detail.isLoading && !data}
      extra={
        data ? (
          <Space size={8}>
            <StatusBadge status={data.status} size="default" />
            <Tooltip title="刷新" placement="top">
              <Button type="text" aria-label="刷新" icon={<ReloadOutlined />} onClick={() => detail.refetch()} />
            </Tooltip>
          </Space>
        ) : undefined
      }
    >
      {detail.isError ? <ErrorAlert error={detail.error} title="任务详情加载失败" /> : null}
      {data ? (
        <Flex vertical>
          <Descriptions
            size="small"
            column={2}
            items={[
              { key: "job_id", label: "job_id", span: 2, children: <Mono>{data.job_id}</Mono> },
              { key: "job_key", label: "job_key", span: 2, children: <Mono secondary>{data.job_key}</Mono> },
              { key: "dataset", label: "数据集", children: <Mono>{data.dataset}</Mono> },
              { key: "scope", label: "范围", children: <Mono>{data.scope}</Mono> },
              {
                key: "window",
                label: "窗口",
                children: <Mono>{data.window_start ? `${data.window_start} ~ ${data.window_end}` : "—"}</Mono>,
              },
              { key: "attempt", label: "尝试", children: <Num muted>{`${data.attempt}/${data.max_attempts}`}</Num> },
              { key: "scheduled", label: "计划时间", children: <TimeText value={data.scheduled_at} /> },
              { key: "started", label: "开始时间", children: <TimeText value={data.started_at} /> },
              { key: "finished", label: "结束时间", children: <TimeText value={data.finished_at} /> },
              {
                key: "duration",
                label: "耗时",
                children: <Mono secondary>{formatDuration(data.started_at, data.finished_at)}</Mono>,
              },
              {
                key: "rows",
                label: "写入行数",
                children:
                  data.rows_written === null ? (
                    <Mono secondary>—</Mono>
                  ) : (
                    <Num>{data.rows_written.toLocaleString("zh-CN")}</Num>
                  ),
              },
              { key: "worker", label: "worker", children: <Mono secondary>{data.worker ?? "—"}</Mono> },
              { key: "request", label: "request_id", span: 2, children: <Mono secondary>{data.request_id ?? "—"}</Mono> },
            ]}
          />
          {data.error ? (
            <>
              <SectionDivider title="错误" />
              <ErrorText text={data.error} />
            </>
          ) : null}
        </Flex>
      ) : null}
    </Drawer>
  );
}
