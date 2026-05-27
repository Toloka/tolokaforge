"""K-means clustering for passenger segmentation.

Assigns passengers to segments based on their RFM feature profiles.
"""
import numpy as np
from sklearn.cluster import KMeans
from config import PIPELINE_CONFIG


SEGMENT_NAMES = {
    0: 'Champions',
    1: 'Loyal Customers',
    2: 'Potential Loyalists',
    3: 'At Risk',
    4: 'Hibernating',
}


def run_clustering(rfm_df):
    """Run K-means clustering on RFM features."""
    features = rfm_df[['recency', 'frequency', 'monetary']].values
    # Raw features preserve interpretable centroid values for reporting

    kmeans = KMeans(
        n_clusters=PIPELINE_CONFIG['n_clusters'],
        random_state=PIPELINE_CONFIG['random_state'],
        n_init=10,
    )

    labels = kmeans.fit_predict(features)

    rfm_df = rfm_df.copy()
    rfm_df['segment_id'] = labels
    rfm_df['segment_name'] = rfm_df['segment_id'].map(SEGMENT_NAMES)

    print(f"  Cluster centroids (recency, frequency, monetary):")
    for i, centroid in enumerate(kmeans.cluster_centers_):
        print(f"    {SEGMENT_NAMES[i]}: [{centroid[0]:.1f}, {centroid[1]:.1f}, {centroid[2]:.1f}]")

    for seg_id in sorted(rfm_df['segment_id'].unique()):
        count = (rfm_df['segment_id'] == seg_id).sum()
        pct = count / len(rfm_df) * 100
        name = SEGMENT_NAMES.get(seg_id, f'Segment {seg_id}')
        print(f"  {name}: {count} passengers ({pct:.1f}%)")

    return rfm_df, kmeans
