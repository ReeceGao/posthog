import { useActions, useValues } from 'kea'
import { LemonTable, LemonTableColumn, LemonTableColumns } from 'lib/lemon-ui/LemonTable'
import { NotebookListItemType } from '~/types'
import { Link } from 'lib/lemon-ui/Link'
import { urls } from 'scenes/urls'
import { createdAtColumn, createdByColumn } from 'lib/lemon-ui/LemonTable/columnUtils'
import { LemonButton, LemonInput, LemonSelect, LemonTag } from '@posthog/lemon-ui'
import { DEFAULT_FILTERS, notebooksListLogic } from '../Notebook/notebooksListLogic'
import { useEffect } from 'react'
import { LemonBanner } from 'lib/lemon-ui/LemonBanner'
import { LemonMenu } from 'lib/lemon-ui/LemonMenu'
import { IconDelete, IconEllipsis } from 'lib/lemon-ui/icons'
import { notebookPopoverLogic } from '../Notebook/notebookPopoverLogic'
import { membersLogic } from 'scenes/organization/Settings/membersLogic'
import { ContainsTypeFilters } from 'scenes/notebooks/NotebooksList/ContainsTypeFilter'

export function NotebooksTable(): JSX.Element {
    const { notebooksAndTemplates, filters, notebooksLoading, notebookTemplates, filteredNotebooksLoading } =
        useValues(notebooksListLogic)
    const { loadNotebooks, setFilters } = useActions(notebooksListLogic)
    const { meFirstMembers } = useValues(membersLogic)
    const { setVisibility, selectNotebook } = useActions(notebookPopoverLogic)

    useEffect(() => {
        loadNotebooks()
    }, [])

    const columns: LemonTableColumns<NotebookListItemType> = [
        {
            title: 'Title',
            dataIndex: 'title',
            width: '100%',
            render: function Render(title, { short_id, is_template }) {
                return (
                    <Link
                        data-attr="notebook-title"
                        to={urls.notebook(short_id)}
                        className="font-semibold flex items-center gap-2"
                    >
                        {title || 'Untitled'}
                        {is_template && <LemonTag type="highlight">TEMPLATE</LemonTag>}
                    </Link>
                )
            },
            sorter: (a, b) => (a.title ?? 'Untitled').localeCompare(b.title ?? 'Untitled'),
        },
        createdByColumn<NotebookListItemType>() as LemonTableColumn<
            NotebookListItemType,
            keyof NotebookListItemType | undefined
        >,
        createdAtColumn<NotebookListItemType>() as LemonTableColumn<
            NotebookListItemType,
            keyof NotebookListItemType | undefined
        >,
        {
            render: function Render(_, notebook) {
                return (
                    <LemonMenu
                        items={[
                            {
                                items: [
                                    {
                                        label: 'Delete',
                                        icon: <IconDelete />,
                                        status: 'danger',

                                        onClick: () => {
                                            notebooksListLogic.actions.deleteNotebook(
                                                notebook.short_id,
                                                notebook?.title
                                            )
                                        },
                                    },
                                ],
                            },
                        ]}
                        actionable
                    >
                        <LemonButton aria-label="more" icon={<IconEllipsis />} status="stealth" size="small" />
                    </LemonMenu>
                )
            },
        },
    ]

    return (
        <div className="space-y-4">
            <LemonBanner
                type="info"
                action={{
                    onClick: () => {
                        selectNotebook(notebookTemplates[0].short_id)
                        setVisibility('visible')
                    },
                    children: 'Get started',
                }}
            >
                <b>Welcome to the preview of Notebooks</b> - a great way to bring Insights, Replays, Feature Flags and
                many more PostHog prodcuts together into one place.
            </LemonBanner>
            <div className="flex justify-between gap-2 flex-wrap">
                <LemonInput
                    type="search"
                    placeholder="Search for notebooks"
                    onChange={(s) => {
                        setFilters({ search: s })
                    }}
                    value={filters.search}
                />
                <div className="flex items-center gap-4 flex-wrap">
                    <ContainsTypeFilters filters={filters} setFilters={setFilters} />
                    <div className="flex items-center gap-2">
                        <span>Created by:</span>
                        <LemonSelect
                            options={[
                                { value: DEFAULT_FILTERS.createdBy as string, label: DEFAULT_FILTERS.createdBy },
                                ...meFirstMembers.map((x) => ({
                                    value: x.user.uuid,
                                    label: x.user.first_name,
                                })),
                            ]}
                            size="small"
                            value={filters.createdBy}
                            onChange={(v): void => {
                                setFilters({ createdBy: v || DEFAULT_FILTERS.createdBy })
                            }}
                            dropdownMatchSelectWidth={false}
                        />
                    </div>
                </div>
            </div>
            <LemonTable
                data-attr="dashboards-table"
                pagination={{ pageSize: 100 }}
                dataSource={notebooksAndTemplates}
                rowKey="short_id"
                columns={columns}
                loading={notebooksLoading || filteredNotebooksLoading}
                defaultSorting={{ columnKey: '-created_at', order: 1 }}
                emptyState={`No notebooks matching your filters!`}
                nouns={['notebook', 'notebooks']}
            />
        </div>
    )
}
