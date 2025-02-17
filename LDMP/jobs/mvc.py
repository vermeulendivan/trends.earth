import functools
import os
import typing
import uuid
from pathlib import Path

from qgis.core import QgsProject
from qgis.PyQt import QtCore
from qgis.PyQt import QtGui
from qgis.PyQt import QtWidgets
from qgis.PyQt import uic
from qgis.utils import iface
from te_schemas.jobs import JobStatus
from te_schemas.results import Band as JobBand
from te_schemas.results import RasterResults
from te_schemas.results import TimeSeriesTableResult

from . import manager
from .. import layers
from .. import metadata
from .. import metadata_dialog
from .. import openFolder
from .. import utils
from ..conf import Setting
from ..conf import settings_manager
from ..data_io import DlgDataIOAddLayersToMap
from ..datasets_dialog import DatasetDetailsDialogue
from ..logger import log
from ..plot import DlgPlotTimeries
from ..reports.mvc import DatasetReportHandler
from ..select_dataset import DlgSelectDataset
from ..utils import FileUtils
from .models import Job
from .models import SortField
from .models import TypeFilter

WidgetDatasetItemUi, _ = uic.loadUiType(
    str(Path(__file__).parents[1] / "gui/WidgetDatasetItem.ui")
)

ICON_PATH = os.path.join(os.path.dirname(__file__), os.path.pardir, "icons")


class JobsModel(QtCore.QAbstractItemModel):
    _relevant_jobs: typing.List[Job]

    def __init__(self, job_manager: manager.JobManager, parent=None):
        super().__init__(parent)
        self._relevant_jobs = job_manager.relevant_jobs

    def index(
        self, row: int, column: int, parent: QtCore.QModelIndex
    ) -> QtCore.QModelIndex:
        invalid_index = QtCore.QModelIndex()

        if self.hasIndex(row, column, parent):
            try:
                job = self._relevant_jobs[row]
                result = self.createIndex(row, column, job)
            except IndexError:
                result = invalid_index
        else:
            result = invalid_index

        return result

    def parent(self, index: QtCore.QModelIndex) -> QtCore.QModelIndex:
        return QtCore.QModelIndex()

    def rowCount(self, index: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return len(self._relevant_jobs)

    def columnCount(self, index: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        return 1

    def data(
        self,
        index: QtCore.QModelIndex = QtCore.QModelIndex(),
        role: QtCore.Qt.ItemDataRole = QtCore.Qt.DisplayRole,
    ) -> typing.Optional[Job]:
        result = None

        if index.isValid():
            job = index.internalPointer()

            if role == QtCore.Qt.DisplayRole or role == QtCore.Qt.ItemDataRole:
                result = job

        return result

    def flags(
        self, index: QtCore.QModelIndex = QtCore.QModelIndex()
    ) -> QtCore.Qt.ItemFlags:
        if index.isValid():
            flags = super().flags(index)
            result = QtCore.Qt.ItemIsEditable | flags
        else:
            result = QtCore.Qt.NoItemFlags

        return result


class JobsSortFilterProxyModel(QtCore.QSortFilterProxyModel):
    current_sort_field: typing.Optional[SortField]
    type_filter: TypeFilter

    def __init__(self, current_sort_field: SortField, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_sort_field = current_sort_field

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex):
        jobs_model = self.sourceModel()
        index = jobs_model.index(source_row, 0, source_parent)
        job: Job = jobs_model.data(index)
        reg_exp = self.filterRegExp()

        matches_filter = (
            reg_exp.exactMatch(job.visible_name)
            or reg_exp.exactMatch(job.local_context.area_of_interest_name)
            or reg_exp.exactMatch(str(job.id))
        )

        matches_type = True
        if self.type_filter == TypeFilter.RASTER:
            matches_type = not job.is_vector()
        elif self.type_filter == TypeFilter.VECTOR:
            matches_type = job.is_vector()

        return matches_filter and matches_type

    def lessThan(self, left: QtCore.QModelIndex, right: QtCore.QModelIndex) -> bool:
        model = self.sourceModel()
        left_job: Job = model.data(left)
        right_job: Job = model.data(right)
        to_sort = (left_job, right_job)

        if self.current_sort_field == SortField.DATE:
            result = sorted(to_sort, key=lambda j: j.start_date)[0] == left_job
        else:
            raise NotImplementedError

        return result

    def set_type_filter(self, filter_type):
        self.type_filter = filter_type


class JobItemDelegate(QtWidgets.QStyledItemDelegate):
    current_index: typing.Optional[QtCore.QModelIndex]
    main_dock: "MainWidget"

    def __init__(
        self,
        main_dock: "MainWidget",
        parent: QtCore.QObject = None,
    ):
        super().__init__(parent)
        self.parent = parent
        self.main_dock = main_dock
        self.current_index = None

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ):
        # get item and manipulate painter basing on idetm data
        proxy_model: QtCore.QSortFilterProxyModel = index.model()
        source_index = proxy_model.mapToSource(index)
        source_model = source_index.model()
        item = source_model.data(source_index, QtCore.Qt.DisplayRole)

        # if a Dataset => show custom widget

        if isinstance(item, Job):
            # get default widget used to edit data
            editor_widget = self.createEditor(self.parent, option, index)
            editor_widget.setGeometry(option.rect)
            pixmap = editor_widget.grab()
            del editor_widget
            painter.drawPixmap(option.rect.x(), option.rect.y(), pixmap)
        else:
            super().paint(painter, option, index)

    def sizeHint(
        self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex
    ):
        proxy_model: QtCore.QSortFilterProxyModel = index.model()
        source_index = proxy_model.mapToSource(index)
        source_model = source_index.model()
        item = source_model.data(source_index, QtCore.Qt.DisplayRole)

        if isinstance(item, Job):
            # parent set to none otherwise remain painted in the widget
            widget = self.createEditor(None, option, index)
            size = widget.size()
            del widget

            return size

        return super().sizeHint(option, index)

    def createEditor(
        self,
        parent: QtWidgets.QWidget,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ):
        # get item and manipulate painter basing on item data
        proxy_model: QtCore.QSortFilterProxyModel = index.model()
        source_index = proxy_model.mapToSource(index)
        source_model = source_index.model()
        item = source_model.data(source_index, QtCore.Qt.DisplayRole)

        # item = model.data(index, QtCore.Qt.DisplayRole)

        if isinstance(item, Job):
            return DatasetEditorWidget(item, self.main_dock, parent=parent)
        else:
            return super().createEditor(parent, option, index)

    def updateEditorGeometry(
        self,
        editor: QtWidgets.QWidget,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ):
        editor.setGeometry(option.rect)


class DatasetEditorWidget(QtWidgets.QWidget, WidgetDatasetItemUi):
    job: Job
    main_dock: "MainWidget"

    add_to_canvas_pb: QtWidgets.QToolButton
    notes_la: QtWidgets.QLabel
    delete_tb: QtWidgets.QToolButton
    download_tb: QtWidgets.QToolButton
    name_la: QtWidgets.QLabel
    open_details_tb: QtWidgets.QToolButton
    metadata_pb: QtWidgets.QPushButton
    open_directory_tb: QtWidgets.QToolButton
    plot_tb: QtWidgets.QToolButton
    progressBar: QtWidgets.QProgressBar
    load_tb: QtWidgets.QToolButton
    edit_tb: QtWidgets.QToolButton
    report_pb: QtWidgets.QPushButton

    def __init__(self, job: Job, main_dock: "MainWidget", parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.job = job
        self.main_dock = main_dock
        # allows hiding background prerendered pixmap
        self.setAutoFillBackground(True)

        self.load_data_menu_setup()
        self.add_to_canvas_pb.setMenu(self.load_data_menu)

        self.metadata_menu = QtWidgets.QMenu()
        self.metadata_menu.aboutToShow.connect(self.prepare_metadata_menu)
        self.metadata_pb.setMenu(self.metadata_menu)

        self.open_details_tb.clicked.connect(self.show_details)
        self.open_directory_tb.clicked.connect(self.open_job_directory)
        self.delete_tb.clicked.connect(self.delete_dataset)
        self.load_tb.clicked.connect(self.load_layer)

        self.load_vector_menu_setup()
        self.load_tb.setMenu(self.load_vector_menu)
        self.edit_tb.clicked.connect(self.edit_layer)

        self.delete_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionDeleteSelected.svg"))
        )
        self.open_details_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionPropertiesWidget.svg"))
        )
        self.open_directory_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionFileOpen.svg"))
        )
        self.metadata_pb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "editmetadata.svg"))
        )
        self.add_to_canvas_pb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionAddRasterLayer.svg"))
        )
        self.download_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "cloud-download.svg"))
        )
        self.edit_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionToggleEditing.svg"))
        )
        self.load_tb.setIcon(
            QtGui.QIcon(os.path.join(ICON_PATH, "mActionAddOgrLayer.svg"))
        )

        self.plot_tb.setIcon(FileUtils.get_icon("chart.svg"))
        self.plot_tb.clicked.connect(self.show_time_series_plot)
        self.plot_tb.setEnabled(False)
        self.plot_tb.hide()

        self.report_pb.setIcon(FileUtils.get_icon("report.svg"))
        self._report_handler = DatasetReportHandler(
            self.report_pb, self.job, self.main_dock.iface
        )
        # self.add_to_canvas_pb.setFixedSize(self.open_directory_tb.size())
        # self.add_to_canvas_pb.setMinimumSize(self.open_directory_tb.size())

        if self.job.is_vector():
            self.edit_tb.setEnabled(False)
            layers = QgsProject.instance().mapLayers()
            for l in layers.values():
                # Ensure this vector layer is added to the map
                if l.source().split("|")[0] == str(job.results.vector.uri.uri):
                    self.edit_tb.setEnabled(True)
                    break

        self.name_la.setText(self.job.visible_name)

        area_name = self.job.local_context.area_of_interest_name
        job_start_date = utils.utc_to_local(self.job.start_date).strftime(
            "%Y-%m-%d %H:%M"
        )

        if area_name:
            notes_text = f"{area_name} ({job_start_date})"
        else:
            notes_text = f"{job_start_date}"
        self.notes_la.setText(notes_text)

        self.download_tb.setEnabled(False)

        self.delete_tb.setEnabled(True)

        # set visibility of progress bar and download button
        if self.job.is_vector():
            self.download_tb.hide()
            self.add_to_canvas_pb.hide()
            self.open_details_tb.hide()
            self.progressBar.hide()
        elif self.job.is_file():
            self.download_tb.hide()
            self.add_to_canvas_pb.hide()
            self.metadata_pb.hide()
            self.load_tb.hide()
            self.edit_tb.hide()
            self.progressBar.hide()
        else:
            self.load_tb.hide()
            self.edit_tb.hide()

            if self.job.status in (JobStatus.RUNNING, JobStatus.PENDING):
                self.progressBar.setMinimum(0)
                self.progressBar.setMaximum(0)
                self.progressBar.setFormat("Processing...")
                self.progressBar.show()
                self.download_tb.hide()
                self.add_to_canvas_pb.setEnabled(False)
                if isinstance(self.job.results, TimeSeriesTableResult):
                    self.download_tb.hide()
                    self.plot_tb.show()
                    self.add_to_canvas_pb.hide()
                    self.metadata_pb.hide()
                    self.open_directory_tb.hide()
            elif self.job.status == JobStatus.FINISHED:
                self.progressBar.hide()
                result_auto_download = settings_manager.get_value(
                    Setting.DOWNLOAD_RESULTS
                )

                if result_auto_download:
                    self.download_tb.hide()
                else:
                    self.download_tb.show()
                    self.download_tb.setEnabled(True)
                    self.download_tb.clicked.connect(
                        functools.partial(manager.job_manager.download_job_results, job)
                    )

                self.add_to_canvas_pb.setEnabled(False)
                self.metadata_pb.setEnabled(False)
                if isinstance(self.job.results, TimeSeriesTableResult):
                    self.plot_tb.setEnabled(True)
                    self.plot_tb.show()
                    self.download_tb.hide()
                    self.add_to_canvas_pb.hide()
                    self.metadata_pb.hide()
                    self.open_directory_tb.hide()
            elif self.job.status in (JobStatus.DOWNLOADED, JobStatus.GENERATED_LOCALLY):
                self.progressBar.hide()
                self.download_tb.hide()
                self.add_to_canvas_pb.setEnabled(self.has_loadable_result())
                self.metadata_pb.setEnabled(self.has_loadable_result())

        # Initialize dataset report handler
        self._report_handler.init()

    def has_loadable_result(self):
        result = False

        if self.job.results is not None:
            if self.job.results.uri and (
                manager.is_gdal_vsi_path(self.job.results.uri.uri)
                or (
                    self.job.results.uri.uri.suffix in [".vrt", ".tif"]
                    and self.job.results.uri.uri.exists()
                )
            ):
                result = True

        return result

    def show_details(self):
        self.main_dock.pause_scheduler()
        DatasetDetailsDialogue(self.job, parent=iface.mainWindow()).exec_()
        self.main_dock.resume_scheduler()

    def show_metadata(self, file_path):
        self.main_dock.pause_scheduler()
        ds_metadata = metadata.read_qmd(file_path)
        dlg = metadata_dialog.DlgDatasetMetadata(self)
        dlg.set_metadata(ds_metadata)
        dlg.exec_()
        ds_metadata = dlg.get_metadata()
        metadata.save_qmd(file_path, ds_metadata)
        self.main_dock.resume_scheduler()

    def open_job_directory(self):
        job_directory = manager.job_manager.get_job_file_path(self.job).parent
        # NOTE: not using QDesktopServices.openUrl here, since it seems to not be
        # working correctly (as of Jun 2021 on Ubuntu)
        openFolder(str(job_directory))

    def load_data_menu_setup(self):
        self.load_data_menu = QtWidgets.QMenu()
        action_add_default_data_to_map = self.load_data_menu.addAction(
            self.tr("Add default layers from this dataset to map")
        )
        action_add_default_data_to_map.triggered.connect(self.load_dataset)
        action_choose_layers_to_add_to_map = self.load_data_menu.addAction(
            self.tr("Select specific layers from this dataset to add to map...")
        )
        action_choose_layers_to_add_to_map.triggered.connect(
            self.load_dataset_choose_layers
        )

    def load_dataset(self):
        manager.job_manager.display_default_job_results(self.job)

    def delete_dataset(self):
        self.main_dock.pause_scheduler()
        utils.delete_dataset(self.job)
        self.main_dock.resume_scheduler()

    def load_dataset_choose_layers(self):
        self.main_dock.pause_scheduler()
        dialogue = DlgDataIOAddLayersToMap(self, self.job)
        dialogue.exec_()
        self.main_dock.resume_scheduler()

    def load_layer(self):
        manager.job_manager.display_error_recode_layer(self.job)
        self.edit_tb.setEnabled(True)

    def show_time_series_plot(self):
        table = self.job.results.table
        if len(table) == 0:
            self.main_dock.iface.messageBar().pushMessage(
                self.tr("Time series table is empty"), level=1, duration=5
            )
            return

        data = [x for x in table if x["name"] == "mean"][0]
        base_title = self.tr("Time Series")
        if self.job.task_name:
            task_name = self.job.task_name
        else:
            task_name = ""
        dlg_plot = DlgPlotTimeries(self.main_dock.iface.mainWindow())
        self.set_widget_title(dlg_plot, base_title)
        labels = {
            "title": task_name,
            "bottom": self.tr("Time"),
            "left": [self.tr("Integrated NDVI"), self.tr("NDVI x 10000")],
        }
        dlg_plot.plot_data(data["time"], data["y"], labels)
        dlg_plot.exec_()

    def set_widget_title(self, widget: QtWidgets.QWidget, base_title: str = None):
        # Convenient function for setting the title of a widget.
        if not base_title:
            base_title = widget.windowTitle()

        if self.job.task_name:
            task_name = self.job.task_name
        else:
            task_name = ""

        if not task_name:
            win_title = base_title
        else:
            win_title = f"{base_title} - {task_name}"

        widget.setWindowTitle(win_title)

    def edit_layer(self):
        if not self.has_connected_data():
            self.main_dock.pause_scheduler()
            dlg = DlgSelectDataset(self, validate_all=True)
            self.set_widget_title(dlg)
            if dlg.exec_() == QtWidgets.QDialog.Accepted:
                prod = dlg.prod_band()
                lc = dlg.lc_band()
                soil = dlg.soil_band()
                sdg = dlg.sdg_band()

                if prod:
                    self.job.params["prod"] = {
                        "path": str(prod.path),
                        "band": prod.band_index,
                        "band_name": prod.band_info.name,
                        "uuid": str(prod.job.id),
                    }

                if lc:
                    self.job.params["lc"] = {
                        "path": str(lc.path),
                        "band": lc.band_index,
                        "band_name": lc.band_info.name,
                        "uuid": str(lc.job.id),
                    }

                if soil:
                    self.job.params["soil"] = {
                        "path": str(soil.path),
                        "band": soil.band_index,
                        "band_name": soil.band_info.name,
                        "uuid": str(soil.job.id),
                    }

                manager.job_manager.write_job_metadata_file(self.job)
                manager.job_manager.display_error_recode_layer(self.job)

                band_datas = [
                    {
                        "path": str(prod.path.as_posix()),
                        "name": prod.band_info.name,
                        "out_name": "land_productivity",
                        "index": prod.band_index,
                    },
                    {
                        "path": str(lc.path.as_posix()),
                        "name": lc.band_info.name,
                        "out_name": "land_cover",
                        "index": lc.band_index,
                    },
                    {
                        "path": str(soil.path.as_posix()),
                        "name": soil.band_info.name,
                        "out_name": "soil_organic_carbon",
                        "index": soil.band_index,
                    },
                    {
                        "path": str(sdg.path.as_posix()),
                        "name": sdg.band_info.name,
                        "out_name": "sdg",
                        "index": sdg.band_index,
                    },
                ]
                log("setting default stats value")
                layers.set_default_stats_value(
                    str(self.job.results.vector.uri.uri), band_datas
                )

                manager.job_manager.edit_error_recode_layer(self.job)
                self.main_dock.resume_scheduler()
        else:
            manager.job_manager.display_error_recode_layer(self.job)
            manager.job_manager.edit_error_recode_layer(self.job)

    def has_connected_data(self):
        has_prod = True if "prod" in self.job.params else False
        has_lc = True if "lc" in self.job.params else False
        has_soil = True if "soil" in self.job.params else False

        return has_prod and has_lc and has_soil

    def prepare_metadata_menu(self):
        self.metadata_menu.clear()

        file_path = (
            os.path.splitext(manager.job_manager.get_job_file_path(self.job))[0]
            + ".qmd"
        )
        action = self.metadata_menu.addAction(self.tr("Dataset metadata"))
        action.triggered.connect(lambda _, x=file_path: self.show_metadata(x))
        self.metadata_menu.addSeparator()

        if self.job.results is not None and isinstance(self.job.results, RasterResults):
            for raster in self.job.results.rasters.values():
                file_path = os.path.splitext(raster.uri.uri)[0] + ".qmd"
                action = self.metadata_menu.addAction(
                    self.tr("{} metadata").format(os.path.split(raster.uri.uri)[1])
                )
                action.triggered.connect(lambda _, x=file_path: self.show_metadata(x))

    @property
    def report_handler(self):
        """
        Returns handler with helper methods for generating and viewing
        reports.
        """
        return self._report_handler

    def load_vector_menu_setup(self):
        self.load_vector_menu = QtWidgets.QMenu()
        action_add_vector_to_map = self.load_vector_menu.addAction(
            self.tr("Add vector layer to map")
        )
        action_add_vector_to_map.triggered.connect(self.load_layer)
        action_add_rasters_to_map = self.load_vector_menu.addAction(
            self.tr("Add raster layers to map")
        )
        action_add_rasters_to_map.triggered.connect(self.load_rasters_layers)

    def load_rasters_layers(self):
        jobs = manager.job_manager._known_downloaded_jobs.copy()
        self._load_raster(jobs, "sdg")
        self._load_raster(jobs, "prod")
        self._load_raster(jobs, "soil")
        self._load_raster(jobs, "lc")

    def _load_raster(self, jobs, name):
        if name in self.job.params:
            data = self.job.params[name]
            job = (
                jobs[uuid.UUID(data["uuid"])]
                if uuid.UUID(data["uuid"]) in jobs
                else None
            )
            if job:
                band = job.results.get_bands()[data["band"] - 1]
                layers.add_layer(
                    str(data["path"]), int(data["band"]), JobBand.Schema().dump(band)
                )
