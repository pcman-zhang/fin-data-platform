import { FolderOutlined, TableOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Badge, Card, Descriptions, Empty, Flex, Input, Skeleton, Space, Splitter, Table, Tag, Tooltip, Tree, Typography } from "antd";
import type { DataNode } from "antd/es/tree";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api";
import type { DatasetSummary, FieldOut } from "../api";
import { ErrorAlert, Mono, SectionDivider } from "../ui";

function formatUnknown(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function Datasets() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("dataset");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<React.Key[]>([]);

  const list = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    const specs = (list.data ?? []).filter(
      (spec) =>
        !keyword ||
        spec.dataset.toLowerCase().includes(keyword) ||
        spec.description.toLowerCase().includes(keyword),
    );
    const groups = new Map<string, DatasetSummary[]>();
    for (const spec of specs) {
      groups.set(spec.domain, [...(groups.get(spec.domain) ?? []), spec]);
    }
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [list.data, query]);

  const treeData: DataNode[] = useMemo(
    () =>
      filtered.map(([domain, specs]) => ({
        key: `domain:${domain}`,
        title: `${domain}（${specs.length}）`,
        icon: <FolderOutlined />,
        children: specs.map((spec) => ({
          key: `dataset:${spec.dataset}`,
          title: spec.dataset.split(".").slice(1).join("."),
          icon: <TableOutlined />,
        })),
      })),
    [filtered],
  );

  const allDomainKeys = useMemo(
    () => (list.data ?? []).map((spec) => `domain:${spec.domain}`).filter((key, index, keys) => keys.indexOf(key) === index),
    [list.data],
  );

  useEffect(() => {
    if (allDomainKeys.length > 0) {
      setExpanded((prev) => (prev.length > 0 ? prev : allDomainKeys));
    }
  }, [allDomainKeys]);

  const matchedDomainKeys = useMemo(() => filtered.map(([domain]) => `domain:${domain}`), [filtered]);
  const expandedKeys = query.trim() ? matchedDomainKeys : expanded;

  const open = (name: string) => {
    const next = new URLSearchParams(params);
    next.set("dataset", name);
    setParams(next);
  };

  return (
    <Splitter style={{ minHeight: 520 }} onResizeEnd={() => undefined}>
      <Splitter.Panel defaultSize={280} min={220} max={420} style={{ paddingInlineEnd: 8 }}>
        <Card
          variant="borderless"
          title="数据集"
          extra={<Typography.Text type="secondary">{filtered.length} 项</Typography.Text>}
          style={{ height: "100%" }}
        >
          <Input.Search
            allowClear
            placeholder="搜索数据集 / 描述"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onSearch={setQuery}
            style={{ marginBottom: 12 }}
          />
          {list.isError ? <ErrorAlert error={list.error} title="数据集加载失败" /> : null}
          {list.isLoading ? (
            <Skeleton active paragraph={{ rows: 6 }} title={false} />
          ) : treeData.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无匹配数据集" />
          ) : (
            <Tree
              blockNode
              showIcon
              treeData={treeData}
              expandedKeys={expandedKeys}
              onExpand={(keys) => setExpanded(keys)}
              selectedKeys={selected ? [`dataset:${selected}`] : []}
              onSelect={(keys) => {
                const key = keys[0];
                if (typeof key !== "string") return;
                if (key.startsWith("dataset:")) {
                  open(key.slice("dataset:".length));
                } else if (key.startsWith("domain:")) {
                  setExpanded((prev) => (prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]));
                }
              }}
            />
          )}
        </Card>
      </Splitter.Panel>
      <Splitter.Panel style={{ paddingInlineStart: 8 }}>
        {selected ? (
          <DatasetDetail name={selected} />
        ) : (
          <Card variant="borderless">
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="从左侧选择一个数据集查看字段、口径与血缘" />
          </Card>
        )}
      </Splitter.Panel>
    </Splitter>
  );
}

function DatasetDetail({ name }: { name: string }) {
  const detail = useQuery({ queryKey: ["dataset", name], queryFn: () => api.dataset(name) });

  if (detail.isLoading) {
    return (
      <Card variant="borderless">
        <Skeleton active paragraph={{ rows: 8 }} />
      </Card>
    );
  }
  if (detail.isError) {
    return (
      <Card variant="borderless">
        <ErrorAlert error={detail.error} title={`数据集加载失败：${name}`} />
      </Card>
    );
  }
  if (!detail.data) return null;

  const data = detail.data;
  return (
    <Card variant="borderless">
      <Flex justify="space-between" align="flex-start" gap={16} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            {data.dataset}
          </Typography.Title>
          <Typography.Text type="secondary">{data.description}</Typography.Text>
        </div>
        <Space wrap>
          <Tag>{data.domain}</Tag>
          <Tag>PIT {data.pit_class}</Tag>
          <Tag>v{data.semantic_version}</Tag>
        </Space>
      </Flex>

      <SectionDivider title="元信息" />
      <Descriptions
        size="medium"
        bordered
        column={2}
        items={[
          { key: "grain", label: "粒度", children: data.grain },
          { key: "frequency", label: "更新频率", children: data.update_sla.frequency ?? data.update_frequency },
          { key: "business_key", label: "业务键", children: <Mono>{data.business_key.join(", ")}</Mono> },
          { key: "physical_key", label: "物理键", children: <Mono>{data.physical_key.join(", ")}</Mono> },
          { key: "table", label: "存储表", children: <Mono>{data.canonical_table}</Mono> },
          { key: "read_model", label: "读模型", children: <Mono>{data.read_model}</Mono> },
          {
            key: "available",
            label: "可用窗口",
            children: (
              <Mono>{`${data.update_sla.earliest_available ?? "—"} ~ ${data.update_sla.latest_available ?? "—"}`}</Mono>
            ),
          },
          { key: "partition", label: "分区策略", children: data.partition_strategy },
        ]}
      />

      <SectionDivider title={`字段（${data.fields.length}）`} />
      <Table<FieldOut>
        rowKey="name"
        dataSource={data.fields}
        size="medium"
        pagination={false}
        scroll={{ x: "max-content" }}
        columns={[
          { title: "字段", dataIndex: "name", width: 180, render: (value: string) => <Mono>{value}</Mono> },
          {
            title: "类型",
            dataIndex: "type",
            width: 140,
            render: (value: string, field) =>
              field.precision !== null ? `${value}(${field.precision},${field.scale ?? 0})` : value,
          },
          { title: "单位", dataIndex: "unit", width: 90, render: (value: string | null) => value ?? "—" },
          { title: "PIT", dataIndex: "pit_role", width: 110, render: (value: string) => <Tag>{value}</Tag> },
          { title: "可空", dataIndex: "nullable", width: 80, render: (value: boolean) => (value ? "是" : "否") },
          {
            title: "口径",
            dataIndex: "description",
            ellipsis: { showTitle: false },
            render: (value: string) => (
              <Tooltip title={value} placement="topLeft">
                <Typography.Text type="secondary" style={{ maxWidth: 420 }}>
                  {value}
                </Typography.Text>
              </Tooltip>
            ),
          },
        ]}
      />

      <SectionDivider title={`质量规则（${data.quality.length}）`} />
      {data.quality.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="未登记质量规则" />
      ) : (
        <Flex vertical gap={10}>
          {data.quality.map((item, index) => {
            const { rule, severity, ...rest } = item;
            const params = Object.keys(rest).length > 0 ? JSON.stringify(rest) : "";
            return (
              <Flex key={index} align="center" gap={10} wrap>
                <Badge status={severity === "error" ? "error" : "warning"} text={String(severity ?? "warn")} />
                <Mono>{String(rule)}</Mono>
                {params ? <Typography.Text type="secondary" style={{ fontSize: 12 }}>{params}</Typography.Text> : null}
              </Flex>
            );
          })}
        </Flex>
      )}

      <SectionDivider title="血缘与存储" />
      <Descriptions
        size="medium"
        bordered
        column={2}
        items={[
          {
            key: "upstream",
            label: "上游",
            children:
              data.lineage.upstream.length > 0 ? (
                <Mono>{data.lineage.upstream.map((ref) => ref.dataset).join(", ")}</Mono>
              ) : (
                "源数据"
              ),
          },
          { key: "transform", label: "转换", children: data.lineage.transform },
          {
            key: "partition",
            label: "分区",
            children: <Mono>{`${formatUnknown(data.storage.partition_strategy)} / ${formatUnknown(data.storage.partition_interval)}`}</Mono>,
          },
          { key: "compression", label: "压缩", children: <Mono>{formatUnknown(data.storage.compression)}</Mono> },
          {
            key: "sources",
            label: "采集源",
            children: <Mono>{data.sources.map((source) => `${source.provider}:${source.endpoint}`).join(", ") || "—"}</Mono>,
          },
          {
            key: "derived",
            label: "派生登记",
            children: <Mono>{data.derived.map((entry) => String(entry.algorithm_id)).join(", ") || "—"}</Mono>,
          },
        ]}
      />
    </Card>
  );
}
