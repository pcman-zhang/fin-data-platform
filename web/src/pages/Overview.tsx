import { useQuery } from "@tanstack/react-query";
import { Button, Card, Col, Descriptions, Empty, Flex, Row, Skeleton, Space, Statistic, Table, Typography, theme } from "antd";
import { Link } from "react-router-dom";

import { api, JOB_STATUSES } from "../api";
import type { Watermark } from "../api";
import { ErrorAlert, Mono, Num, StatusBadge, TimeText, paginationConfig, usePageSize } from "../ui";

export default function Overview() {
  const { token } = theme.useToken();
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  const entities = useQuery({
    queryKey: ["entities", "overview"],
    queryFn: () => api.entities({ limit: 1 }),
  });
  const jobs = useQuery({
    queryKey: ["jobs", "overview"],
    queryFn: () => api.jobs({ limit: 500 }),
  });
  const watermarks = useQuery({ queryKey: ["watermarks"], queryFn: api.watermarks });

  const runs = jobs.data ?? [];
  const abnormal = runs.filter((run) => run.status === "failed" || run.status === "dead").length;
  const counts = JOB_STATUSES.map((status) => ({
    status,
    count: runs.filter((run) => run.status === status).length,
  })).filter((item) => item.count > 0);

  const [pageSize, setPageSize] = usePageSize("fdp.pageSize.watermarks", 20);
  const error = health.error ?? datasets.error ?? entities.error ?? jobs.error ?? watermarks.error;

  return (
    <Flex vertical gap={16}>
      {error ? <ErrorAlert error={error} /> : null}

      <Row gutter={16}>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="数据集" value={datasets.data?.length} loading={datasets.isLoading} />
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="实体" value={entities.data?.total} loading={entities.isLoading} />
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic title="水位记录" value={watermarks.data?.length} loading={watermarks.isLoading} />
          </Card>
        </Col>
        <Col span={6}>
          <Card variant="borderless">
            <Statistic
              title="异常任务（最近 500 条）"
              value={abnormal}
              loading={jobs.isLoading}
              styles={{ content: abnormal > 0 ? { color: token.colorError } : undefined }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16}>
        <Col span={8}>
          <Card variant="borderless" title="健康" style={{ height: "100%" }}>
            {health.isLoading ? (
              <Skeleton active paragraph={{ rows: 3 }} title={false} />
            ) : health.data ? (
              <Descriptions
                column={1}
                size="small"
                items={Object.entries(health.data.checks).map(([key, value]) => ({
                  key,
                  label: <Mono secondary>{key}</Mono>,
                  children: (
                    <Space size={8}>
                      <Num>{(value as boolean) ? "通过" : "未通过"}</Num>
                      <StatusBadge status={(value as boolean) ? "succeeded" : "failed"} />
                    </Space>
                  ),
                }))}
              />
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="健康信息不可用" />
            )}
          </Card>
        </Col>
        <Col span={16}>
          <Card
            variant="borderless"
            title="任务状态（最近 500 条）"
            style={{ height: "100%" }}
            extra={
              <Link to="/jobs">
                <Button type="link" size="small">
                  查看任务 →
                </Button>
              </Link>
            }
          >
            {jobs.isLoading ? (
              <Skeleton active paragraph={{ rows: 2 }} title={false} />
            ) : counts.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无运行记录" />
            ) : (
              <Flex wrap gap={24}>
                {counts.map(({ status, count }) => (
                  <Space key={status} size={10}>
                    <StatusBadge status={status} />
                    <Num>{count}</Num>
                  </Space>
                ))}
              </Flex>
            )}
          </Card>
        </Col>
      </Row>

      <Card variant="borderless" title="数据水位" extra={<Typography.Text type="secondary">按数据集 / 范围的最新水位</Typography.Text>}>
        <Table<Watermark>
          rowKey={(mark) => `${mark.dataset}:${mark.scope}`}
          dataSource={watermarks.data ?? []}
          loading={watermarks.isLoading}
          size="medium"
          pagination={{ ...paginationConfig(pageSize, setPageSize), hideOnSinglePage: true }}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无水位记录" />,
          }}
          columns={[
            { title: "数据集", dataIndex: "dataset", width: 260, render: (value: string) => <Mono>{value}</Mono> },
            { title: "范围", dataIndex: "scope", width: 140, render: (value: string) => <Mono secondary>{value || "—"}</Mono> },
            { title: "水位", dataIndex: "watermark_time", render: (value: string | null) => <TimeText value={value} /> },
          ]}
        />
      </Card>
    </Flex>
  );
}
