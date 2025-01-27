// Import necessary components from SciChart
/*
import { 
    SciChartSurface, 
    NumericAxis3D, 
    ScatterRenderableSeries3D, 
    XyzDataSeries3D, 
    SpherePointMarker3D, 
    OrbitModifier3D
} from "scichart";
*/
/*
async function drawChart() {
    // Initialize the SciChartSurface3D
    const { sciChart3DSurface, wasmContext } = await SciChartSurface.create("chart-container");

    // Add 3D axes
    sciChart3DSurface.xAxis = new NumericAxis3D(wasmContext);
    sciChart3DSurface.yAxis = new NumericAxis3D(wasmContext);
    sciChart3DSurface.zAxis = new NumericAxis3D(wasmContext);

    // Create a 3D data series
    const dataSeries = new XyzDataSeries3D(wasmContext);

    // Fetch data from the REST API
    try {
        const response = await fetch("http://localhost:8080/api/data");
        const jsonData = await response.json();

        // Process JSON data and append to the data series
        jsonData.data.forEach(entry => {
            dataSeries.append(entry.x, entry.y, entry.z);
        });

        // Add scatter series with the fetched data
        const scatterSeries = new ScatterRenderableSeries3D(wasmContext, {
            pointMarker: new SpherePointMarker3D(wasmContext, {
                size: 5,
                fill: "#FF6600",
            }),
            dataSeries,
        });

        sciChart3DSurface.renderableSeries.add(scatterSeries);

        // Add interaction modifiers (orbit and zoom)
        sciChart3DSurface.chartModifiers.add(
            new OrbitModifier3D()
        );

    } catch (error) {
        console.error("Error fetching data from the server:", error);
    }
}*/

/*
async function drawChart() {
    const { sciChart3DSurface, wasmContext } = await SciChartSurface.create("chart-container");

    // Add axes
    sciChart3DSurface.xAxis = new NumericAxis3D(wasmContext);
    sciChart3DSurface.yAxis = new NumericAxis3D(wasmContext);
    sciChart3DSurface.zAxis = new NumericAxis3D(wasmContext);

    // Add data series
    const dataSeries = new XyzDataSeries3D(wasmContext);
    dataSeries.append(1, 2, 3);
    dataSeries.append(4, 5, 6);

    // Add scatter series
    const scatterSeries = new ScatterRenderableSeries3D(wasmContext, {
        pointMarker: new SpherePointMarker3D(wasmContext, {
            size: 5,
            fill: "#FF6600",
        }),
        dataSeries,
    });

    sciChart3DSurface.renderableSeries.add(scatterSeries);
}
    */

import { SciChartSurface, NumericAxis, FastLineRenderableSeries, XyDataSeries } from "scichart";

async function drawChart() {
    const { sciChartSurface, wasmContext } = await SciChartSurface.create("chart-container");

    sciChartSurface.xAxis = new NumericAxis(wasmContext);
    sciChartSurface.yAxis = new NumericAxis(wasmContext);

    const dataSeries = new XyDataSeries(wasmContext);
    dataSeries.append(1, 2);
    dataSeries.append(3, 4);

    const lineSeries = new FastLineRenderableSeries(wasmContext, {
        dataSeries,
    });

    sciChartSurface.renderableSeries.add(lineSeries);
}

drawChart();

