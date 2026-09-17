import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Flex,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, formatDateTime } from "../api";
import type { EntitySummary } from "../api";
import {
  EntityStatusBadge,
  ErrorAlert,
  Mono,
  SectionDivider,
  TimeText,
  rowActivate,
  useDrawerWidth,
  usePageSize,
} from "../ui";

const ENTITY_TYPES = [
  { value: "equity", label: "equity" },
  { value: "issuer", label: "issuer" },
  { value: "etf", label: "etf" },
  { value: "lof", label: "lof" },
  { value: "fund", label: "fund" },
  { value: "index", label: "index" },
];

export default function Entities() {
  const [params, setParams] = useSearchParams();
  const selectedParam = params.get("entity");
  const selected = selectedParam ? Number(selectedParam) : null;

  const [query, setQuery] = useState("");
  const [entityType, setEntityType] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePageSize("fdp.pageSize.entities");

  const list = useQuery({
    queryKey: ["entities", query, entityType, page, pageSize],
    queryFn: () =>
      api.entities({
        query: query || undefined,
        entity_type: entityType || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      }),
  });

  const open = (entityId: number) => {
    const next = new URLSearchParams(params);
    next.set("entity", String(entityId));
    setParams(next);
  };
  const close = () => {
    const next = new URLSearchParams(params);
    next.delete("entity");
    setParams(next, { replace: true });
  };

  return (
    <>
      <Card
        variant="borderless"
        title="实体注册表"
        extra={
          <Space size={8} wrap>
            <Select
              allowClear
              options={ENTITY_TYPES}
              value={entityType || undefined}
              onChange={(value) => {
                setEntityType(value ?? "");
                setPage(1);
              }}
              placeholder="全部类型"
              style={{ width: 160 }}
            />
            <Input.Search
              allowClear
              placeholder="代码 / 名称"
              onSearch={(value) => {
                setQuery(value);
                setPage(1);
              }}
              onChange={(event) => {
                if (event.target.value === "") {
                  setQuery("");
                  setPage(1);
                }
              }}
              style={{ width: 240 }}
            />
            <Tooltip title="刷新" placement="top">
              <Button
                type="text"
                aria-label="刷新"
                icon={<ReloadOutlined />}
                onClick={() => list.refetch()}
              />
            </Tooltip>
          </Space>
        }
        styles={{ body: { paddingTop: 16 } }}
      >
        {list.isError ? <ErrorAlert error={list.error} title="实体列表加载失败" /> : null}
        <Table<EntitySummary>
          rowKey="entity_id"
          dataSource={list.data?.items ?? []}
          loading={list.isLoading}
          size="medium"
          scroll={{ x: "max-content" }}
          onRow={rowActivate((row) => open(row.entity_id))}
          rowClassName={(row) => (row.entity_id === selected ? "ant-table-row-selected" : "")}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无匹配实体" />,
          }}
          pagination={{
            pageSize,
            current: page,
            total: list.data?.total ?? 0,
            showSizeChanger: true,
            pageSizeOptions: ["20", "50", "100"],
            showTotal: (total, range) => `${range[0]}–${range[1]} / ${total}`,
            placement: ["bottomEnd"],
            onChange: (nextPage, size) => {
              if (size !== pageSize) {
                setPageSize(size);
                setPage(1);
              } else {
                setPage(nextPage);
              }
            },
          }}
          columns={[
            {
              title: "ID",
              dataIndex: "entity_id",
              width: 88,
              fixed: "start",
              render: (value: number) => <Typography.Text code>{value}</Typography.Text>,
            },
            {
              title: "代码",
              dataIndex: "code",
              width: 120,
              render: (value: string) => <Mono>{value}</Mono>,
            },
            {
              title: "名称",
              dataIndex: "name",
              ellipsis: { showTitle: false },
              sorter: (a, b) => a.name.localeCompare(b.name, "zh-CN"),
              render: (value: string) => (
                <Tooltip title={value} placement="topLeft">
                  <span>{value}</span>
                </Tooltip>
              ),
            },
            {
              title: "类型",
              dataIndex: "entity_type",
              width: 120,
              render: (value: string, row) => <Tag>{row.entity_class ?? value}</Tag>,
            },
            {
              title: "市场",
              dataIndex: "market",
              width: 100,
              render: (value: string | null) => value ?? "—",
            },
            {
              title: "状态",
              dataIndex: "social_status",
              width: 96,
              render: (value: string | null, row) => (
                <EntityStatusBadge socialStatus={value} validTo={row.valid_to} />
              ),
            },
          ]}
        />
      </Card>

      <EntityDrawer entityId={selected} onClose={close} />
    </>
  );
}

function EntityDrawer({ entityId, onClose }: { entityId: number | null; onClose: () => void }) {
  const { width, resizable } = useDrawerWidth();
  const detail = useQuery({
    queryKey: ["entity", entityId],
    queryFn: () => api.entity(entityId!),
    enabled: entityId !== null,
  });
  const data = detail.data;

  return (
    <Drawer
      open={entityId !== null}
      onClose={onClose}
      title={
        data ? (
          <Space size={8}>
            <Typography.Text strong>{data.name}</Typography.Text>
            <Mono secondary>{data.code}</Mono>
          </Space>
        ) : (
          "实体详情"
        )
      }
      size={width}
      resizable={resizable}
      destroyOnHidden
      loading={detail.isLoading && !data}
      extra={data ? <EntityStatusBadge socialStatus={data.social_status} validTo={data.valid_to} /> : undefined}
    >
      {detail.isError ? <ErrorAlert error={detail.error} title="实体详情加载失败" /> : null}
      {data ? (
        <Flex vertical>
          <Descriptions
            size="small"
            column={2}
            items={[
              { key: "id", label: "entity_id", children: <Typography.Text code>{data.entity_id}</Typography.Text> },
              { key: "type", label: "entity_type", children: <Tag>{data.entity_type}</Tag> },
              {
                key: "class",
                label: "entity_class",
                children: data.entity_class ? <Tag>{data.entity_class}</Tag> : "—",
              },
              { key: "market", label: "市场", children: data.market ?? "—" },
              { key: "currency", label: "币种", children: data.currency ?? "—" },
              { key: "exchange", label: "交易所", children: data.exchange ?? "—" },
              {
                key: "valid",
                label: "有效期",
                children: <Mono secondary>{`${data.valid_from ?? "—"} ~ ${data.valid_to ?? "至今"}`}</Mono>,
              },
              { key: "knowledge", label: "知识时间", children: <TimeText value={data.knowledge_time} /> },
            ]}
          />

          <SectionDivider title={`属性时间轴（${data.history.length} 个版本）`} />
          <Timeline
            items={data.history.map((row) => ({
              key: `${row.version}-${row.valid_from ?? "open"}`,
              children: (
                <div>
                  <Space size={8}>
                    <Typography.Text strong>{row.name}</Typography.Text>
                    <Tag>v{row.version}</Tag>
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                    {`${row.valid_from ?? "—"} ~ ${row.valid_to ?? "至今"} · 知识时间 ${formatDateTime(row.knowledge_time)}`}
                  </Typography.Text>
                </div>
              ),
            }))}
          />

          <SectionDivider title="代码履历" />
          {data.code_history.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无代码变更记录" />
          ) : (
            <Flex vertical gap={8}>
              {data.code_history.map((row) => (
                <Flex key={`${row.code}-${row.version}`} justify="space-between" align="center" gap={12}>
                  <Mono>{row.code}</Mono>
                  <Mono secondary>{`${row.valid_from ?? "—"} ~ ${row.valid_to ?? "至今"}`}</Mono>
                </Flex>
              ))}
            </Flex>
          )}

          <SectionDivider title="关系" />
          {data.relations.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无关系记录" />
          ) : (
            <Flex vertical gap={8}>
              {data.relations.map((relation, index) => (
                <Flex key={index} align="center" gap={8} wrap>
                  {relation.direction === "out" ? <ArrowRightOutlined /> : <ArrowLeftOutlined />}
                  <Tag>{relation.relation_type}</Tag>
                  <Mono>{relation.related_code ?? relation.related_id}</Mono>
                  <Typography.Text type="secondary">{relation.related_name ?? ""}</Typography.Text>
                </Flex>
              ))}
            </Flex>
          )}

          <SectionDivider title="外部标识" />
          {data.external_ids.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无外部标识" />
          ) : (
            <Flex vertical gap={8}>
              {data.external_ids.map((item) => (
                <Flex key={`${item.id_type}-${item.id_value}`} align="center" gap={8}>
                  <SafetyCertificateOutlined />
                  <Tag>{item.id_type}</Tag>
                  <Mono>{item.id_value}</Mono>
                </Flex>
              ))}
            </Flex>
          )}
        </Flex>
      ) : null}
    </Drawer>
  );
}
