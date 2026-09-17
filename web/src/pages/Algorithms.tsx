import { ReloadOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Badge, Button, Card, Col, Empty, Flex, Row, Skeleton, Space, Table, Timeline, Tooltip, Typography } from "antd";

import { api } from "../api";
import type { AlgorithmEventOut, AlgorithmRowOut, DataGenerationOut } from "../api";
import {
  ErrorAlert,
  Mono,
  Num,
  TimeText,
  paginationConfig,
  usePageSize,
} from "../ui";

function statusText(status: string): string {
  return status === "active" ? "active" : "deprecated";
}

export default function Algorithms() {
  const registry = useQuery({ queryKey: ["algorithms"], queryFn: api.algorithms });
  const events = useQuery({ queryKey: ["algorithm-events"], queryFn: api.algorithmEvents });
  const generations = useQuery({ queryKey: ["algorithm-generations"], queryFn: api.generations });

  const [registryPageSize, setRegistryPageSize] = usePageSize("fdp.pageSize.algorithms", 20);
  const [generationPageSize, setGenerationPageSize] = usePageSize("fdp.pageSize.generations", 20);
  const error = registry.error ?? events.error ?? generations.error;

  return (
    <Flex vertical gap={16}>
      {error ? <ErrorAlert error={error} /> : null}

      <Card
        variant="borderless"
        title="算法登记（注册表）"
        extra={
          <Flex align="center" gap={8}>
            <Typography.Text type="secondary">{`${registry.data?.length ?? 0} 项`}</Typography.Text>
            <Tooltip title="刷新" placement="top">
              <Button
                type="text"
                aria-label="刷新"
                icon={<ReloadOutlined />}
                onClick={() => {
                  void registry.refetch();
                  void events.refetch();
                  void generations.refetch();
                }}
              />
            </Tooltip>
          </Flex>
        }
      >
        <Table<AlgorithmRowOut>
          rowKey="algorithm_id"
          dataSource={registry.data ?? []}
          loading={registry.isLoading}
          size="medium"
          scroll={{ x: "max-content" }}
          locale={{
            emptyText: (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description="暂无算法登记（在字典 derived 登记并 @register 后，经 Runtime 同步入库）"
              />
            ),
          }}
          pagination={{ ...paginationConfig(registryPageSize, setRegistryPageSize), hideOnSinglePage: true }}
          columns={[
            {
              title: "algorithm_id",
              dataIndex: "algorithm_id",
              width: 180,
              fixed: "start",
              render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
            },
            {
              title: "版本",
              dataIndex: "version",
              width: 80,
              align: "right",
              render: (value: number) => <Num muted>{`v${value}`}</Num>,
            },
            {
              title: "状态",
              dataIndex: "status",
              width: 120,
              render: (value: string) => (
                <Badge
                  status={value === "active" ? "success" : "default"}
                  text={statusText(value)}
                  size="small"
                />
              ),
            },
            {
              title: "数据集",
              dataIndex: "dataset",
              width: 200,
              ellipsis: { showTitle: false },
              render: (value: string | null) =>
                value ? (
                  <Tooltip title={value} placement="topLeft">
                    <Mono>{value}</Mono>
                  </Tooltip>
                ) : (
                  <Mono secondary>—</Mono>
                ),
            },
            {
              title: "输出",
              dataIndex: "output",
              width: 130,
              render: (value: string | null) => (value ? <Mono>{value}</Mono> : <Mono secondary>—</Mono>),
            },
            {
              title: "输入",
              dataIndex: "inputs",
              ellipsis: { showTitle: false },
              render: (value: string[]) =>
                value.length > 0 ? (
                  <Tooltip title={value.join("\n")} placement="topLeft">
                    <Mono secondary>{value.join(", ")}</Mono>
                  </Tooltip>
                ) : (
                  <Mono secondary>—</Mono>
                ),
            },
            {
              title: "实现",
              dataIndex: "implementation",
              ellipsis: { showTitle: false },
              render: (value: string) => (
                <Tooltip title={value} placement="topLeft">
                  <Mono secondary>{value}</Mono>
                </Tooltip>
              ),
            },
            {
              title: "生效日",
              dataIndex: "effective_from",
              width: 120,
              render: (value: string | null) => <Mono secondary>{value ?? "—"}</Mono>,
            },
            {
              title: "说明",
              dataIndex: "description",
              ellipsis: { showTitle: false },
              render: (value: string) => (
                <Tooltip title={value} placement="topLeft">
                  <Typography.Text type="secondary">{value}</Typography.Text>
                </Tooltip>
              ),
            },
          ]}
        />
      </Card>

      <Row gutter={16}>
        <Col span={10}>
          <Card variant="borderless" title="升级台账" style={{ height: "100%" }}>
            {events.isLoading ? (
              <Skeleton active paragraph={{ rows: 4 }} title={false} />
            ) : (events.data?.length ?? 0) === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无算法升级事件" />
            ) : (
              <Timeline
                items={[...(events.data ?? [])]
                  .sort((a, b) => b.effective_from.localeCompare(a.effective_from))
                  .map((event: AlgorithmEventOut) => ({
                    key: `${event.algorithm_id}-${event.effective_from}`,
                    children: (
                      <div>
                        <Space size={8}>
                          <Typography.Text code>{event.algorithm_id}</Typography.Text>
                          <Mono secondary>{event.effective_from}</Mono>
                        </Space>
                        <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                          {event.reason || "—"}
                        </Typography.Text>
                      </div>
                    ),
                  }))}
              />
            )}
          </Card>
        </Col>
        <Col span={14}>
          <Card variant="borderless" title="投影代次（data_generation）" style={{ height: "100%" }}>
            <Table<DataGenerationOut>
              rowKey="read_model"
              dataSource={generations.data ?? []}
              loading={generations.isLoading}
              size="medium"
              pagination={{ ...paginationConfig(generationPageSize, setGenerationPageSize), hideOnSinglePage: true }}
              locale={{
                emptyText: (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="暂无物化投影（materialize=latest 的派生在重算后记录代次）"
                  />
                ),
              }}
              columns={[
                { title: "投影", dataIndex: "read_model", render: (value: string) => <Mono>{value}</Mono> },
                {
                  title: "代次",
                  dataIndex: "generation",
                  width: 180,
                  render: (value: string) => <Mono secondary>{value}</Mono>,
                },
                {
                  title: "更新时间",
                  dataIndex: "updated_at",
                  width: 180,
                  render: (value: string) => <TimeText value={value} />,
                },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </Flex>
  );
}
