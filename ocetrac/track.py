import xarray as xr
import numpy as np
import scipy.ndimage
from skimage.measure import regionprops 
from skimage.measure import label as label_np
import dask.array as dsa

def _morphological_operations(da, radius=8): 
    '''Converts xarray.DataArray to binary, defines structuring element, and performs morphological closing then opening.
    Parameters
    ----------
    da     : xarray.DataArray
            The data to label
    radius : int
            Length of grid spacing to define the radius of the structing element used in morphological closing and opening.
        
    '''
    
    # Convert images to binary. All positive values == 1, otherwise == 0
    bitmap_binary = da.where(da>0, drop=False, other=0)
    bitmap_binary = bitmap_binary.where(bitmap_binary==0, drop=False, other=1)
    
    # Define structuring element
    diameter = radius*2
    x = np.arange(-radius, radius+1)
    x, y = np.meshgrid(x, x)
    r = x**2+y**2 
    se = r<radius**2

    def binary_open_close(bitmap_binary):
        bitmap_binary_padded = np.pad(bitmap_binary,
                                      ((diameter, diameter), (diameter, diameter)),
                                      mode='wrap')
        s1 = scipy.ndimage.binary_closing(bitmap_binary_padded, se, iterations=1)
        s2 = scipy.ndimage.binary_opening(s1, se, iterations=1)
        unpadded= s2[diameter:-diameter, diameter:-diameter]
        return unpadded
    
    mo_binary = xr.apply_ufunc(binary_open_close, bitmap_binary,
                               input_core_dims=[['lat', 'lon']],
                               output_core_dims=[['lat', 'lon']],
                               output_dtypes=[bitmap_binary.dtype],
                               vectorize=True,
                               dask='parallelized')
    
    return mo_binary

def _apply_mask(mask, binary_images):
    binary_images_with_mask = binary_images.where(mask==1, drop=False, other=0)
    return binary_images_with_mask


def _filter_area(binary_images, min_size_quartile):
    '''calculatre area with regionprops'''
    
    def get_labels(binary_images):
        blobs_labels = _label_either(binary_images, background=0)
        return blobs_labels
    
    labels = xr.apply_ufunc(get_labels, binary_images,
                            input_core_dims=[['lat', 'lon']],
                            output_core_dims=[['lat', 'lon']],
                            output_dtypes=[binary_images.dtype],
                            vectorize=True,
                            dask='parallelized')
    
    
    labels = xr.DataArray(labels, dims=binary_images.dims, coords=binary_images.coords)
    labels = labels.where(labels>0, drop=False, other=np.nan)  
    
    # The labels are repeated each time step, therefore we relabel them to be consecutive
    for i in range(1, labels.shape[0]):
        labels[i,:,:] = labels[i,:,:].values + labels[i-1,:,:].max().values
    
    labels = labels.where(labels>0, drop=False, other=0)  
    labels_wrapped, N_initial = _wrap(np.array(labels))
    
    # Calculate Area of each object and keep objects larger than threshold
    props = regionprops(labels_wrapped.astype('int'))
    labelprops = [p.label for p in props]
    labelprops = xr.DataArray(labelprops, dims=['label'], coords={'label': labelprops}) 
    area = xr.DataArray([p.area for p in props], dims=['label'], coords={'label': labelprops})  # Number of pixels of the region.
    min_area = np.percentile(area, min_size_quartile*100)
    print('minimum area: ', min_area) 
    keep_labels = labelprops.where(area>=min_area, drop=True)
    keep_where = np.isin(labels_wrapped, keep_labels)
    out_labels = xr.DataArray(np.where(keep_where==False, 0, labels_wrapped), dims=binary_images.dims, coords=binary_images.coords)
    
    # Convert images to binary. All positive values == 1, otherwise == 0
    binary_labels = out_labels.where(out_labels==0, drop=False, other=1)
        
    return area, min_area, binary_labels, N_initial


def _label_either(data, **kwargs):
    if isinstance(data, dsa.Array):
        try:
            from dask_image.ndmeasure import label as label_dask
            def label_func(a, **kwargs):
                ids, num = label_dask(a, **kwargs)
                return ids
        except ImportError:
            raise ImportError(
                "Dask_image is required to use this function on Dask arrays. "
                "Either install dask_image or else call .load() on your data."
            )
    else:
        label_func = label_np
    return label_func(data, **kwargs)


def _wrap(labels):
    ''' Impose periodic boundary and wrap labels'''
    first_column = labels[..., 0]
    last_column = labels[..., -1]
    
    unique_first = np.unique(first_column[first_column>0])
    
    # This loop iterates over the unique values in the first column, finds the location of those values in 
    # the first columnm and then uses that index to replace the values in the last column with the first column value
    for i in enumerate(unique_first):
        first = np.where(first_column == i[1])
        last = last_column[first[0], first[1]]
        bad_labels = np.unique(last[last>0])
        replace = np.isin(labels, bad_labels)
        labels[replace] = i[1]

    labels_wrapped = np.unique(labels, return_inverse=True)[1].reshape(labels.shape)
    
    # recalculate the total number of labels 
    N = np.max(labels_wrapped)

    return labels_wrapped, N


def track(da, mask, radius=8, min_size_quartile=0.75):
    '''Image labeling and tracking.
    
    Parameters
    ----------
    da : xarray.DataArray
        The data to label.
    
    radius : int
        size of the structuring element used in morphological opening and closing. Radius specified by the number of grid units.
        
    area_quantile : float
        quantile used to define the threshold of the smallest area object retained in tracking. Value should be between 0 and 1.
        
    mask : xarray.DataArray
        The mask of ponts to ignore. Must be binary where 1 = true point and 0 = background to be ignored. 
        
    Returns
    -------
    labels : xarray.DataArray
        Integer labels of the connected regions.
    '''
        
    # Convert data to binary, define structuring element, and perform morphological closing then opening
    binary_images = _morphological_operations(da, radius=radius)
    
    # Apply mask
    binary_images_with_mask  = _apply_mask(mask, binary_images)
    
    # Filter area
    area, min_area, binary_labels, N_initial = _filter_area(binary_images_with_mask, min_size_quartile)
    
    # Label objects
    labels, num = _label_either(binary_labels, return_num= True, connectivity=3)
    
    # Wrap labels
    labels_wrapped, N_final = _wrap(labels)
    
    # Final labels to DataArray
    new_labels = xr.DataArray(labels_wrapped, dims=da.dims, coords=da.coords)   
    new_labels = new_labels.where(new_labels!=0, drop=False, other=np.nan)

    
    ## Metadata
    
    # Calculate Percent of total object area retained after size filtering
    sum_tot_area = int(np.sum(area.values))
    
    reject_area = area.where(area<=min_area, drop=True)
    sum_reject_area = int(np.sum(reject_area.values))
    percent_area_reject = (sum_reject_area/sum_tot_area)
    
    accept_area = area.where(area>min_area, drop=True)
    sum_accept_area = int(np.sum(accept_area.values))
    percent_area_accept = (sum_accept_area/sum_tot_area)

    new_labels = new_labels.rename('labels')
    new_labels.attrs['inital objects identified'] = int(N_initial)
    new_labels.attrs['final objects tracked'] = int(N_final)
    new_labels.attrs['radius'] = radius
    new_labels.attrs['size quantile threshold'] = min_size_quartile
    new_labels.attrs['min area'] = min_area
    new_labels.attrs['percent area reject'] = percent_area_reject
    new_labels.attrs['percent area accept'] = percent_area_accept
    
    print('inital objects identified \t', int(N_initial))
    print('final objects tracked \t', int(N_final))
    
    return new_labels